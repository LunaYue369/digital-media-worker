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
    """执行生成 pipeline（支持完整生成和局部修改）

    局部修改由 params["modify_scope"] 控制：
    - modify_scope.title: True=重写标题, False=保留
    - modify_scope.content: True=重写正文和标签, False=保留
    - modify_scope.images: "keep"=全部保留, "all"=全部重做, [1,3]=只重做指定编号
    - modify_scope.video: "keep"=保留, "redo"=重做
    不存在 modify_scope 时走完整生成流程。
    """
    thread_ts = sess["thread_ts"]
    channel = sess["channel"]
    params = sess["params"]
    user_images = sess["user_images"]
    draft = sess.get("draft") or {}
    modify_scope = params.pop("modify_scope", None)
    modify_feedback = params.pop("modify_feedback", "")

    # 判断是否为局部修改模式
    is_partial = bool(modify_scope and draft)

    if is_partial:
        log.info("Pipeline 局部修改: scope=%s", modify_scope)
        redo_title = modify_scope.get("title", False)
        redo_content = modify_scope.get("content", False)
        redo_images = modify_scope.get("images", "keep")  # "keep" / "all" / [1,3]
        redo_video = modify_scope.get("video", "keep")     # "keep" / "redo"
    else:
        log.info("Pipeline 完整生成: user_images=%s, params=%s",
                 user_images, {k: v for k, v in params.items() if k != "image"})
        redo_title = True
        redo_content = True
        redo_images = "all"
        redo_video = "redo" if params.get("generate_video") else "keep"

    redo_any_copy = redo_title or redo_content

    # ── 分析图片处理需求 ──────────────────────────────────────

    image_mode = params.get("image_mode", "")
    per_image_modes = params.get("per_image_modes", [])
    extra_generate_count = params.get("extra_generate_count", 0)
    need_video = (redo_video == "redo")

    # 局部修改时，如果图片保留则不需要任何图片处理
    if is_partial and redo_images == "keep":
        need_any_ai_image = False
        need_enhance = False
        need_reference = False
        need_generate = False
    else:
        if image_mode == "mixed" and per_image_modes:
            need_enhance = "enhance" in per_image_modes
            need_reference = "reference" in per_image_modes
            need_generate = extra_generate_count > 0
        elif image_mode == "enhance":
            need_enhance = True
            need_reference = False
            need_generate = extra_generate_count > 0
        elif image_mode == "reference":
            need_enhance = False
            need_reference = True
            need_generate = extra_generate_count > 0
        elif image_mode == "generate":
            need_enhance = False
            need_reference = False
            need_generate = True
        elif image_mode == "raw":
            need_enhance = False
            need_reference = False
            need_generate = extra_generate_count > 0
        else:
            need_enhance = bool(user_images)
            need_reference = False
            need_generate = not user_images or extra_generate_count > 0

        need_any_ai_image = need_enhance or need_reference or need_generate

    # ── 0. 生成媒体提示词（如果需要 AI 图片或视频） ───────────

    enhance_prompt = ""
    reference_prompt = ""
    image_prompt = ""
    video_prompt = ""

    if need_any_ai_image or need_video:
        say(text="[0/4] 构思创意概念中...", thread_ts=thread_ts)
        prompts, prompt_usage = generate_prompts(
            params=params,
            session_id=thread_ts,
            need_enhance=need_enhance,
            need_reference=need_reference,
            need_generate=need_generate,
            need_video=need_video,
            has_reference_images=bool(user_images),
        )
        session.add_usage(thread_ts, prompt_usage["prompt"], prompt_usage["completion"], prompt_usage["cost"])
        enhance_prompt = prompts.get("enhance_prompt", "")
        reference_prompt = prompts.get("reference_prompt", "")
        image_prompt = prompts.get("image_prompt", "")
        video_prompt = prompts.get("video_prompt", "")

    # 计算总步骤数
    steps_needed = (
        (1 if redo_any_copy else 0)
        + (1 if need_any_ai_image or (redo_images != "keep" and user_images) else 0)
        + (1 if need_video else 0)
        + 1  # 发送结果
    )
    step = 0

    # ── 1. 文案生成 + 审核循环 ────────────────────────────────

    if redo_any_copy:
        # 确定重写模式
        if redo_title and redo_content:
            rewrite_mode = "full"
            step_label = "正在撰写文案..."
        elif redo_title:
            rewrite_mode = "title_only"
            step_label = "正在重写标题..."
        else:
            rewrite_mode = "content_only"
            step_label = "正在重写正文..."

        step += 1
        say(text=f"[{step}/{steps_needed}] {step_label}", thread_ts=thread_ts)

        previous_copy = draft.get("copy") if is_partial else None
        copy_dict = None
        review = None
        max_rounds = get_max_rounds()

        for round_num in range(1, max_rounds + 1):
            if round_num == 1:
                copy_dict, usage = write_copy(
                    params, {}, thread_ts,
                    previous_copy=previous_copy,
                    rewrite_mode=rewrite_mode,
                    user_feedback=modify_feedback,
                )
            else:
                feedback = build_feedback(review)
                copy_dict, usage = write_copy(
                    params, {}, thread_ts,
                    feedback=feedback, previous_copy=copy_dict,
                )

            session.add_usage(thread_ts, usage["prompt"], usage["completion"], usage["cost"])
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
    else:
        # 保留现有文案
        copy_dict = draft.get("copy", {})
        log.info("保留现有文案: %s", copy_dict.get("title", "")[:30])

    # ── 2. 图片处理 ──────────────────────────────────────────

    if is_partial and redo_images == "keep":
        # 全部保留
        image_paths = draft.get("images", [])
        log.info("保留全部现有图片: %d 张", len(image_paths))

    elif is_partial and isinstance(redo_images, list):
        # 局部重做：只重做指定图片，每张按各自 mode 处理，其余保留
        # redo_images 格式: [{"index": 2, "mode": "enhance"}, {"index": 3, "mode": "generate"}]
        old_images = draft.get("images", [])
        redo_map = {}  # {index: mode}
        for item in redo_images:
            if isinstance(item, dict):
                redo_map[item["index"]] = item.get("mode", "generate")
            elif isinstance(item, int):
                redo_map[item] = "generate"  # 兼容旧格式 [1, 3]

        step += 1
        indices_str = ", ".join(str(i) for i in sorted(redo_map))
        say(text=f"[{step}/{steps_needed}] 正在重新处理第 {indices_str} 张图片...",
            thread_ts=thread_ts)

        # 按需生成 prompt（不同 mode 需要不同 prompt）
        redo_modes = set(redo_map.values())
        if ("generate" in redo_modes or "reference" in redo_modes) and not image_prompt:
            prompts, prompt_usage = generate_prompts(
                params=params, session_id=thread_ts,
                need_generate="generate" in redo_modes,
                need_reference="reference" in redo_modes,
                need_enhance="enhance" in redo_modes,
                need_video=False,
                has_reference_images=bool(user_images),
            )
            session.add_usage(thread_ts, prompt_usage["prompt"], prompt_usage["completion"], prompt_usage["cost"])
            image_prompt = prompts.get("image_prompt", "") or image_prompt
            enhance_prompt = prompts.get("enhance_prompt", "") or enhance_prompt
            reference_prompt = prompts.get("reference_prompt", "") or reference_prompt

        # 逐张处理
        image_paths = []
        for idx, old_path in enumerate(old_images):
            user_idx = idx + 1  # 转为用户编号（从1开始）
            if user_idx not in redo_map:
                image_paths.append(old_path)
                continue

            mode = redo_map[user_idx]
            log.info("局部重做 图%d: mode=%s", user_idx, mode)

            if mode == "raw" and user_idx <= len(user_images):
                # 用原始上传图片替换
                image_paths.append(user_images[user_idx - 1])

            elif mode == "enhance" and user_idx <= len(user_images):
                # 用原始上传图片重新美化
                new_paths = process_images(
                    user_images=[user_images[user_idx - 1]],
                    params={**params, "image_mode": "enhance"},
                    session_id=thread_ts,
                    enhance_prompt=enhance_prompt,
                )
                image_paths.append(new_paths[0] if new_paths else old_path)

            elif mode == "reference" and user_idx <= len(user_images):
                # 以原始上传图片为参考重新生成
                new_paths = process_images(
                    user_images=[user_images[user_idx - 1]],
                    params={**params, "image_mode": "reference"},
                    session_id=thread_ts,
                    reference_prompt=reference_prompt,
                )
                image_paths.append(new_paths[0] if new_paths else old_path)

            else:
                # generate 模式，或找不到原始图片时 fallback 到 generate
                new_paths = process_images(
                    user_images=[],
                    params={**params, "image_mode": "generate", "image_count": 1},
                    session_id=thread_ts,
                    image_prompt=image_prompt,
                )
                image_paths.append(new_paths[0] if new_paths else old_path)

            if image_paths[-1] != old_path:
                log.info("第 %d 张图片已重新处理 (%s)", user_idx, mode)
            else:
                log.warning("第 %d 张图片处理失败，保留原图", user_idx)

    else:
        # 全部重做（完整生成或 redo_images="all"）
        image_paths = []
        if need_any_ai_image or user_images:
            step += 1
            say(text=f"[{step}/{steps_needed}] 正在处理图片...", thread_ts=thread_ts)
            image_paths = process_images(
                user_images=user_images,
                params=params,
                session_id=thread_ts,
                image_prompt=image_prompt,
                enhance_prompt=enhance_prompt,
                reference_prompt=reference_prompt,
            )

    # ── 2.5 保底校验：小红书要求至少一张图片 ──────────────────
    if not image_paths and not need_video:
        log.warning("无图片素材，自动触发 AI 生成一张兜底图片")
        step += 1
        say(text=f"[{step}/{steps_needed}] 未检测到图片素材，正在用 AI 自动生成一张...", thread_ts=thread_ts)

        if not image_prompt:
            prompts, prompt_usage = generate_prompts(
                params=params, session_id=thread_ts,
                need_generate=True, need_video=False,
                has_reference_images=False,
            )
            session.add_usage(thread_ts, prompt_usage["prompt"], prompt_usage["completion"], prompt_usage["cost"])
            image_prompt = prompts.get("image_prompt", "")

        fallback_params = {**params, "image_mode": "generate", "image_count": 1}
        image_paths = process_images(
            user_images=[],
            params=fallback_params,
            session_id=thread_ts,
            image_prompt=image_prompt,
        )

    # ── 3. 视频生成（可选） ──────────────────────────────────

    if need_video:
        step += 1
        say(text=f"[{step}/{steps_needed}] 正在生成视频（可能需要几分钟）...", thread_ts=thread_ts)
        video_path = generate_video(
            user_images=user_images,
            params=params,
            session_id=thread_ts,
            video_prompt=video_prompt,
        )
    elif is_partial:
        # 保留现有视频
        video_path = draft.get("video")
    else:
        video_path = None

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
