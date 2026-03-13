"""宣传素材生成 Pipeline — 主流程编排

完整流程：
1. 媒体提示词生成（prompt_engineer）
2. 文案生成 → 审核循环（最多 3 轮）
3. 图片处理（AI 图生图润色 / AI 文生图）
4. 视频生成（可选，Seedance 文生视频 / 图生视频）
5. 发送结果到 Slack + 操作按钮
6. 自动发布到小红书（可选，通过 Slack 按钮触发）
"""

import logging

from core import session
from core.session import REVIEWING
from agents.copywriter import write_copy
from agents.reviewer import review_copy, build_feedback, get_max_rounds
from agents.media_engineer import generate_prompts
from services.image_processor import process_images
from services.video_generator import generate_video
from services.usage_tracker import estimate_cost
from core.merchant_config import get as _merchant_get
from services.xhs_publisher import publish_to_xhs, PublishConfig, PublishResult
from slack_ui.blocks import build_result_message

log = logging.getLogger(__name__)


def run_pipeline(sess: dict, say, slack_client):
    """执行完整生成 pipeline"""
    thread_ts = sess["thread_ts"]
    channel = sess["channel"]
    params = sess["params"]
    user_images = sess["user_images"]
    log.info("Pipeline 开始: user_images=%s, params=%s", user_images, {k: v for k, v in params.items() if k != "image"})

    # 判断是否需要 AI 生成图片/视频
    image_mode = params.get("image_mode", "reference" if user_images else "generate")
    need_ai_image = image_mode in ("reference", "generate")
    need_video = params.get("generate_video", False)

    # ── 0. 生成媒体提示词（如果需要 AI 图片或视频） ───────────

    image_prompt = ""
    video_prompt = ""

    if need_ai_image or need_video:
        say(text="[0/4] 构思创意概念中...", thread_ts=thread_ts)
        prompts, prompt_usage = generate_prompts(
            params=params,
            session_id=thread_ts,
            need_image=need_ai_image,
            need_video=need_video,
            has_reference_images=bool(user_images),
        )
        session.add_usage(thread_ts, prompt_usage["prompt"], prompt_usage["completion"], prompt_usage["cost"])
        image_prompt = prompts.get("image_prompt", "")
        video_prompt = prompts.get("video_prompt", "")

    # 计算总步骤数
    total_steps = 2 + (1 if need_ai_image or user_images else 0) + (1 if need_video else 0)
    step = 0

    # ── 1. 文案生成 + 审核循环 ────────────────────────────────

    step += 1
    say(text=f"[{step}/{total_steps}] 正在撰写文案...", thread_ts=thread_ts)

    copy_dict = None
    review = None
    max_rounds = get_max_rounds()

    for round_num in range(1, max_rounds + 1):
        if round_num == 1:
            copy_dict, usage = write_copy(params, {}, thread_ts)
        else:
            feedback = build_feedback(review)
            copy_dict, usage = write_copy(
                params, {}, thread_ts,
                feedback=feedback, previous_copy=copy_dict,
            )

        session.add_usage(thread_ts, usage["prompt"], usage["completion"], usage["cost"])

        # 审核
        review = review_copy(copy_dict, params, thread_ts)

        if review.get("approved"):
            log.info("文案审核通过 (第 %d 轮)", round_num)
            if round_num > 1:
                say(text=f"文案经过 {round_num} 轮审核后通过。", thread_ts=thread_ts)
            break
        else:
            log.info("文案审核未通过 (第 %d/%d 轮): %s", round_num, max_rounds, review.get("verdict"))
    else:
        log.warning("文案 %d 轮审核均未通过，使用最终版本", max_rounds)

    # ── 2. 图片处理 ──────────────────────────────────────────

    image_paths = []
    if need_ai_image or user_images:
        step += 1
        say(text=f"[{step}/{total_steps}] 正在处理图片...", thread_ts=thread_ts)
        image_paths = process_images(
            user_images=user_images,
            params=params,
            session_id=thread_ts,
            image_prompt=image_prompt,
        )

    # ── 2.5 保底校验：小红书要求至少一张图片 ──────────────────
    # 如果前面没有走图片处理流程（既没有用户图片也没有 AI 生成），
    # 或者图片处理后结果为空，自动用 AI 根据文案生成一张兜底图片。
    if not image_paths and not need_video:
        log.warning("无图片素材，自动触发 AI 生成一张兜底图片")
        step += 1
        say(text=f"[{step}/{total_steps}] 未检测到图片素材，正在用 AI 自动生成一张...", thread_ts=thread_ts)

        # 如果之前没有生成过 image_prompt，先生成一个
        if not image_prompt:
            prompts, prompt_usage = generate_prompts(
                params=params,
                session_id=thread_ts,
                need_image=True,
                need_video=False,
                has_reference_images=False,
            )
            session.add_usage(thread_ts, prompt_usage["prompt"], prompt_usage["completion"], prompt_usage["cost"])
            image_prompt = prompts.get("image_prompt", "")

        # 强制用 generate 模式生成 1 张图片
        fallback_params = {**params, "image_mode": "generate", "image_count": 1}
        image_paths = process_images(
            user_images=[],
            params=fallback_params,
            session_id=thread_ts,
            image_prompt=image_prompt,
        )

    # ── 3. 视频生成（可选） ──────────────────────────────────

    video_path = None
    if need_video:
        step += 1
        say(text=f"[{step}/{total_steps}] 正在生成视频（可能需要几分钟）...", thread_ts=thread_ts)
        video_path = generate_video(
            user_images=user_images,
            params=params,
            session_id=thread_ts,
            video_prompt=video_prompt,
        )

    # ── 4. 保存草稿并发送结果 ─────────────────────────────────

    step += 1
    sess["draft"] = {
        "copy": copy_dict,
        "images": image_paths,
        "video": video_path,
    }

    # 上传图片和视频到 Slack
    _upload_media_to_slack(slack_client, channel, thread_ts, image_paths, video_path)

    # 发送文案 + 操作按钮
    blocks = build_result_message(copy_dict, image_paths, video_path, sess["usage"])
    say(blocks=blocks, text="宣传草稿已就绪！", thread_ts=thread_ts)

    # 切换到审核状态
    session.update_stage(thread_ts, REVIEWING)


def _upload_media_to_slack(slack_client, channel: str, thread_ts: str,
                           image_paths: list, video_path: str | None):
    """上传图片和视频到 Slack 频道"""
    for path in image_paths:
        try:
            slack_client.files_upload_v2(
                channel=channel,
                file=path,
                thread_ts=thread_ts,
                initial_comment="",
            )
        except Exception as e:
            log.error("上传图片失败 %s: %s", path, e)

    if video_path:
        try:
            slack_client.files_upload_v2(
                channel=channel,
                file=video_path,
                thread_ts=thread_ts,
                initial_comment="",
            )
        except Exception as e:
            log.error("上传视频失败 %s: %s", video_path, e)


# ---------------------------------------------------------------------------
# 小红书发布
# ---------------------------------------------------------------------------

def publish_draft_to_xhs(
    sess: dict,
    say,
    account: str | None = None,
    post_time: str | None = None,
) -> PublishResult:
    """将已生成的草稿发布到小红书

    由 Slack action handler 调用（用户点击"发布到小红书"按钮时触发）。

    Args:
        sess: 当前 session dict（需包含 draft）
        say: Slack say 函数
        account: 小红书账号名称，None 用默认
        post_time: 定时发布时间，None 立即发布

    Returns:
        PublishResult
    """
    thread_ts = sess["thread_ts"]
    draft = sess.get("draft")

    if not draft:
        say(text="未找到草稿，请先生成内容。", thread_ts=thread_ts)
        return PublishResult(success=False, status="ERROR", error="未找到草稿")

    copy_dict = draft["copy"]
    image_paths = draft.get("images", [])
    video_path = draft.get("video")

    title = copy_dict.get("title", "")
    content = copy_dict.get("content", "")
    tags = copy_dict.get("tags", [])

    # ── 校验：小红书不支持纯文字发布，必须有图片或视频 ──
    if not video_path and not image_paths:
        say(
            text="发布失败：小红书不支持纯文字发布，至少需要一张图片。\n"
                 "请重新生成内容并确保包含图片素材。",
            thread_ts=thread_ts,
        )
        return PublishResult(
            success=False, status="ERROR",
            error="缺少图片素材，小红书要求至少一张图片",
        )

    # 规范 tags 格式，确保以 "#" 开头
    if tags and not tags[0].startswith("#"):
        tags = [f"#{t}" for t in tags]

    say(text="正在发布到小红书...", thread_ts=thread_ts)

    config = PublishConfig(
        headless=True,
        account=account,
        post_time=post_time,
        location=_merchant_get("xhs_location"),
    )

    result = publish_to_xhs(
        title=title,
        content=content,
        image_paths=image_paths if not video_path else None,
        video_path=video_path,
        tags=tags,
        config=config,
    )

    if result.success:
        msg = "已成功发布到小红书！"
        if result.note_link:
            msg += f"\n{result.note_link}"
        say(text=msg, thread_ts=thread_ts)
    else:
        say(
            text=f"发布失败：{result.status} — {result.error}",
            thread_ts=thread_ts,
        )

    return result
