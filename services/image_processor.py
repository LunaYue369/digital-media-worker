"""图片处理服务 — 支持三种模式

模式判断逻辑（由 conversation.py 对话层的 params 决定）：
1. raw:       用户上传图片，直接使用原图不做任何处理
2. reference: 用户上传图片作为参考 → Seedream 图生图（AI 润色）
3. generate:  用户无图片 → Seedream 文生图

params["image_mode"] 的值：
- "raw"       → 直接使用用户原图
- "reference" → 用户图片作为参考，AI 润色美化
- "generate"  → 无参考，AI 纯文生图
- 未设置时默认：有图就 "reference"，无图就 "generate"
"""

import logging
from datetime import datetime
from pathlib import Path

from core.merchant_config import fallback_image_prompt as _fallback_image_prompt
from services.seedream_client import SeedreamClient

log = logging.getLogger(__name__)

# 输出目录
OUTPUT_DIR = Path.home() / "Desktop" / "media"


def process_images(user_images: list, params: dict, session_id: str,
                   image_prompt: str = "") -> list[str]:
    """处理图片，返回输出文件路径列表

    Args:
        user_images: 用户上传的图片路径列表
        params: 生成参数（含 image_mode, promotion 等）
        session_id: 会话 ID
        image_prompt: prompt_engineer 生成的图片提示词

    Returns:
        输出图片路径列表
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # 判断图片处理模式
    mode = params.get("image_mode", "")
    if not mode:
        mode = "reference" if user_images else "generate"
    # "edit" 已废弃，统一转为 "reference"
    if mode == "edit":
        mode = "reference"

    if mode == "raw" and user_images:
        # 模式0：直接使用原图，不做任何处理
        log.info("原图直发: %d 张", len(user_images))
        return [str(p) for p in user_images]

    if mode == "reference" and user_images:
        # 模式1：用户图片作为参考，Seedream 图生图
        return _ai_from_reference(user_images, image_prompt, run_dir)

    elif mode == "generate":
        # 模式2：无参考，Seedream 纯文生图
        image_count = params.get("image_count", 1)
        return _ai_generate(image_prompt, run_dir, image_count=image_count)

    else:
        log.info("无图片需要处理")
        return []


# ── 模式1：用户图片作为参考，AI 生成新图 ────────────────────

def _ai_from_reference(user_images: list, image_prompt: str, run_dir: Path) -> list[str]:
    """用 Seedream 图生图，用户图片作为参考"""
    if not image_prompt:
        image_prompt = "基于参考图生成精美的美食宣传图，保留食物主体，优化光线和构图，高清美食摄影"

    client = SeedreamClient()
    all_paths = []
    for i, img in enumerate(user_images):
        p = Path(img)
        size_mb = p.stat().st_size / 1024 / 1024 if p.exists() else -1
        log.info("参考图 %d: %s (%.2f MB)", i + 1, p, size_mb)
        try:
            paths = client.image_to_image(
                prompt=image_prompt,
                image_paths=[p],
                output_dir=run_dir,
            )
            all_paths.extend(paths)
        except Exception as e:
            log.error("图生图失败 (图 %d): %s", i + 1, e)
    log.info("Seedream 图生图完成: %d 张", len(all_paths))
    return [str(p) for p in all_paths]


# ── 模式2：无参考，AI 纯文生图 ─────────────────────────────

def _ai_generate(image_prompt: str, run_dir: Path, image_count: int = 1) -> list[str]:
    """用 Seedream 文生图，纯 AI 生成"""
    if not image_prompt:
        image_prompt = _fallback_image_prompt()

    try:
        client = SeedreamClient()
        if image_count > 1:
            # 多图生成
            paths = client.text_to_images(
                prompt=image_prompt,
                output_dir=run_dir,
                max_images=image_count,
            )
        else:
            paths = client.text_to_image(
                prompt=image_prompt,
                output_dir=run_dir,
            )
        log.info("Seedream 文生图完成: %d 张", len(paths))
        return [str(p) for p in paths]
    except Exception as e:
        log.error("Seedream 文生图失败: %s", e)
        return []
