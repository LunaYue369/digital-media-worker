"""宣传素材生成 Pipeline — 主流程编排

完整流程：
1. 媒体提示词生成（prompt_engineer）
2. 文案生成 → 审核循环（最多 3 轮）
3. 图片处理（Pillow 加工 / AI 图生图 / AI 文生图）
4. 视频生成（可选，Seedance 文生视频 / 图生视频）
5. 发送结果到 Slack + 操作按钮
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
from slack_ui.blocks import build_result_message

log = logging.getLogger(__name__)


def run_pipeline(sess: dict, say, slack_client):
    """执行完整生成 pipeline"""
    thread_ts = sess["thread_ts"]
    channel = sess["channel"]
    params = sess["params"]
    user_images = sess["user_images"]

    # 判断是否需要 AI 生成图片/视频
    image_mode = params.get("image_mode", "edit" if user_images else "generate")
    need_ai_image = image_mode in ("reference", "generate")
    need_video = params.get("generate_video", False)

    # ── 0. 生成媒体提示词（如果需要 AI 图片或视频） ───────────

    image_prompt = ""
    video_prompt = ""

    if need_ai_image or need_video:
        say(text="[0/4] 正在构思创意方案...", thread_ts=thread_ts)
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
                say(text=f"文案经过 {round_num} 轮打磨，已通过审核。", thread_ts=thread_ts)
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

    # ── 3. 视频生成（可选） ──────────────────────────────────

    video_path = None
    if need_video:
        step += 1
        say(text=f"[{step}/{total_steps}] 正在生成视频（这可能需要几分钟）...", thread_ts=thread_ts)
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
    say(blocks=blocks, text="宣传方案已生成！", thread_ts=thread_ts)

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
