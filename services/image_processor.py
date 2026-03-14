"""图片处理服务 — 支持四种模式 + 逐张混合处理

模式（由 conversation.py 对话层的 params 决定）：
1. raw:       直接使用原图，不做任何处理
2. enhance:   AI 美化原图（加字、加滤镜、调色，保留原图主体）
3. reference: 以原图为参考，AI 生成全新图片
4. generate:  无参考图，AI 纯文生图

支持两种使用方式：
- 统一模式：params["image_mode"] 对所有图片统一处理
- 混合模式：params["image_mode"]="mixed" + params["per_image_modes"] 逐张指定
- 额外生成：params["extra_generate_count"] 额外纯文生图（可与任何模式组合）
"""

import logging
from datetime import datetime
from pathlib import Path

from core.merchant_config import fallback_image_prompt as _fallback_image_prompt
from services.seedream_client import SeedreamClient

log = logging.getLogger(__name__)

# 输出目录
OUTPUT_DIR = Path.home() / "Desktop" / "media"

# 默认提示词
_DEFAULT_ENHANCE = "在保留原图主体和构图的基础上，优化画面质感，增强色彩和光线，使其更适合社交媒体宣传"
_DEFAULT_REFERENCE = "参考原图中的食物和场景，生成一张全新的精美美食宣传图，高清美食摄影"


def process_images(user_images: list, params: dict, session_id: str,
                   image_prompt: str = "",
                   enhance_prompt: str = "",
                   reference_prompt: str = "") -> list[str]:
    """处理图片，返回输出文件路径列表

    Args:
        user_images: 用户上传的图片路径列表
        params: 生成参数（含 image_mode, per_image_modes 等）
        session_id: 会话 ID
        image_prompt: 纯文生图提示词（generate 模式用）
        enhance_prompt: 美化提示词（enhance 模式用）
        reference_prompt: 参考生成提示词（reference 模式用）

    Returns:
        输出图片路径列表
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    mode = params.get("image_mode", "")
    # 向后兼容："edit" 已废弃
    if mode == "edit":
        mode = "enhance"

    per_image_modes = params.get("per_image_modes", [])

    # ── 混合模式：逐张处理 ──
    if mode == "mixed" and per_image_modes and user_images:
        result_paths = _process_mixed(
            user_images, per_image_modes, run_dir,
            enhance_prompt, reference_prompt,
        )
    # ── 统一模式 ──
    else:
        if not mode:
            mode = "enhance" if user_images else "generate"

        if mode == "raw" and user_images:
            log.info("原图直发: %d 张", len(user_images))
            result_paths = [str(p) for p in user_images]

        elif mode == "enhance" and user_images:
            result_paths = _ai_enhance(user_images, enhance_prompt or reference_prompt, run_dir)

        elif mode == "reference" and user_images:
            result_paths = _ai_reference(user_images, reference_prompt or image_prompt, run_dir)

        elif mode == "generate":
            image_count = params.get("image_count", 1)
            result_paths = _ai_generate(image_prompt, run_dir, image_count=image_count)

        else:
            log.info("无图片需要处理")
            result_paths = []

    # ── 额外纯文生图 ──
    extra_count = params.get("extra_generate_count", 0)
    if extra_count > 0:
        log.info("额外纯文生图: %d 张", extra_count)
        extra_paths = _ai_generate(image_prompt, run_dir, image_count=extra_count)
        result_paths.extend(extra_paths)

    return result_paths


# ── 混合模式：逐张按各自 mode 处理 ───────────────────────────

def _process_mixed(user_images: list, per_image_modes: list, run_dir: Path,
                   enhance_prompt: str, reference_prompt: str) -> list[str]:
    """混合模式：每张图片按各自的 mode 处理"""
    client = SeedreamClient()
    all_paths = []

    for i, img_path in enumerate(user_images):
        # 如果 per_image_modes 长度不足，默认 enhance
        img_mode = per_image_modes[i] if i < len(per_image_modes) else "enhance"
        p = Path(img_path)
        log.info("混合模式 图%d: %s → %s", i + 1, p.name, img_mode)

        if img_mode == "raw":
            all_paths.append(str(p))

        elif img_mode == "enhance":
            prompt = enhance_prompt or _DEFAULT_ENHANCE
            try:
                paths = client.image_to_image(
                    prompt=prompt, image_paths=[p], output_dir=run_dir,
                )
                all_paths.extend(str(x) for x in paths)
            except Exception as e:
                log.error("美化失败 (图%d): %s", i + 1, e)

        elif img_mode == "reference":
            prompt = reference_prompt or _DEFAULT_REFERENCE
            try:
                paths = client.image_to_image(
                    prompt=prompt, image_paths=[p], output_dir=run_dir,
                )
                all_paths.extend(str(x) for x in paths)
            except Exception as e:
                log.error("参考生成失败 (图%d): %s", i + 1, e)

        else:
            log.warning("未知图片模式 '%s' (图%d)，跳过", img_mode, i + 1)

    log.info("混合模式处理完成: %d 张输出", len(all_paths))
    return all_paths


# ── 美化模式：保留原图主体，AI 增强 ──────────────────────────

def _ai_enhance(user_images: list, enhance_prompt: str, run_dir: Path) -> list[str]:
    """Seedream 图生图 — 美化模式（保留原图主体）"""
    prompt = enhance_prompt or _DEFAULT_ENHANCE

    client = SeedreamClient()
    all_paths = []
    for i, img in enumerate(user_images):
        p = Path(img)
        size_mb = p.stat().st_size / 1024 / 1024 if p.exists() else -1
        log.info("美化图 %d: %s (%.2f MB)", i + 1, p, size_mb)
        try:
            paths = client.image_to_image(
                prompt=prompt, image_paths=[p], output_dir=run_dir,
            )
            all_paths.extend(str(x) for x in paths)
        except Exception as e:
            log.error("美化失败 (图%d): %s", i + 1, e)
    log.info("Seedream 美化完成: %d 张", len(all_paths))
    return all_paths


# ── 参考模式：以原图为灵感，AI 生成全新图 ────────────────────

def _ai_reference(user_images: list, reference_prompt: str, run_dir: Path) -> list[str]:
    """Seedream 图生图 — 参考模式（生成全新图片）"""
    prompt = reference_prompt or _DEFAULT_REFERENCE

    client = SeedreamClient()
    all_paths = []
    for i, img in enumerate(user_images):
        p = Path(img)
        size_mb = p.stat().st_size / 1024 / 1024 if p.exists() else -1
        log.info("参考图 %d: %s (%.2f MB)", i + 1, p, size_mb)
        try:
            paths = client.image_to_image(
                prompt=prompt, image_paths=[p], output_dir=run_dir,
            )
            all_paths.extend(str(x) for x in paths)
        except Exception as e:
            log.error("参考生成失败 (图%d): %s", i + 1, e)
    log.info("Seedream 参考生成完成: %d 张", len(all_paths))
    return all_paths


# ── 纯文生图：无参考，AI 生成 ────────────────────────────────

def _ai_generate(image_prompt: str, run_dir: Path, image_count: int = 1) -> list[str]:
    """Seedream 文生图，纯 AI 生成"""
    if not image_prompt:
        image_prompt = _fallback_image_prompt()

    try:
        client = SeedreamClient()
        if image_count > 1:
            paths = client.text_to_images(
                prompt=image_prompt, output_dir=run_dir, max_images=image_count,
            )
        else:
            paths = client.text_to_image(
                prompt=image_prompt, output_dir=run_dir,
            )
        log.info("Seedream 文生图完成: %d 张", len(paths))
        return [str(p) for p in paths]
    except Exception as e:
        log.error("Seedream 文生图失败: %s", e)
        return []
