"""视频生成服务 — 使用 Seedance 1.5 Pro

支持两种模式：
1. 有用户参考图 → 图生视频（用户图片作为首帧）
2. 无参考图 → 文生视频（纯 AI 生成）

提示词由 prompt_engineer 生成。
"""

import logging
from datetime import datetime
from pathlib import Path

from core.merchant_config import default_product as _default_product, fallback_video_prompt as _fallback_video_prompt
from services.seedance_client import SeedanceClient

log = logging.getLogger(__name__)

OUTPUT_DIR = Path.home() / "Desktop" / "media"


def generate_video(user_images: list, params: dict, session_id: str,
                   video_prompt: str = "") -> str | None:
    """生成宣传视频

    Args:
        user_images: 用户上传的图片路径列表（第一张可用作首帧）
        params: 生成参数
        session_id: 会话 ID
        video_prompt: prompt_engineer 生成的视频提示词

    Returns:
        视频文件路径，失败返回 None
    """
    if not video_prompt:
        video_prompt = _fallback_video_prompt()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "video.mp4"

    try:
        client = SeedanceClient()

        # 从 params 读取用户指定的视频参数（对话层收集）
        duration = params.get("video_duration", 8)
        ratio = params.get("video_ratio", "9:16")

        # 判断模式：有参考图则图生视频，否则文生视频
        if user_images:
            # 用第一张图作为首帧
            ref_image = Path(user_images[0])
            log.info("图生视频: 参考图=%s, 时长=%d秒, 比例=%s", ref_image, duration, ratio)
            client.generate_from_image(
                image_path=ref_image,
                prompt=video_prompt,
                output_path=output_path,
                duration=duration,
                ratio=ratio,
                timeout=600,
            )
        else:
            log.info("文生视频: prompt=%s, 时长=%d秒, 比例=%s", video_prompt[:50], duration, ratio)
            client.generate_from_text(
                prompt=video_prompt,
                output_path=output_path,
                duration=duration,
                ratio=ratio,
                timeout=600,
            )

        log.info("视频生成完成: %s", output_path)
        return str(output_path)

    except Exception as e:
        log.error("视频生成失败: %s", e)
        return None
