"""图片处理服务 — 支持三种模式

模式判断逻辑（由 conversation.py 对话层的 params 决定）：
1. edit_only: 用户上传图片 + 只需要加工（裁剪、加文字）→ Pillow
2. ai_from_reference: 用户上传图片作为参考 + 要 AI 生成新图 → Seedream 图生图
3. ai_generate: 用户无图片 + 要 AI 生成 → Seedream 文生图

params["image_mode"] 的值：
- "edit"      → Pillow 加工原图
- "reference" → 用户图片作为参考，AI 生成新图
- "generate"  → 无参考，AI 纯文生图
- 未设置时默认：有图就 "edit"，无图就 "generate"
"""

import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from services.seedream_client import SeedreamClient

log = logging.getLogger(__name__)

# 小红书推荐尺寸
XHS_SIZE = (1080, 1440)  # 3:4 竖图

# 输出目录
OUTPUT_DIR = Path.home() / "Desktop" / "media"

# 字体路径（Windows 中文字体）
_FONT_PATH = "C:/Windows/Fonts/msyh.ttc"


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
        mode = "edit" if user_images else "generate"

    if mode == "edit" and user_images:
        # 模式1：Pillow 加工原图
        return _pillow_edit(user_images, params, run_dir)

    elif mode == "reference" and user_images:
        # 模式2：用户图片作为参考，Seedream 图生图
        return _ai_from_reference(user_images, image_prompt, run_dir)

    elif mode == "generate":
        # 模式3：无参考，Seedream 纯文生图
        image_count = params.get("image_count", 1)
        return _ai_generate(image_prompt, run_dir, image_count=image_count)

    else:
        log.info("无图片需要处理")
        return []


# ── 模式1：Pillow 加工原图 ─────────────────────────────────

def _pillow_edit(user_images: list, params: dict, run_dir: Path) -> list[str]:
    """用 Pillow 加工用户上传的图片，100% 保真原图"""
    output_paths = []

    for idx, img_path in enumerate(user_images):
        try:
            img = Image.open(img_path)
        except Exception as e:
            log.error("打开图片失败 %s: %s", img_path, e)
            continue

        # 裁剪适配小红书 3:4
        processed = _resize_and_pad(img, XHS_SIZE)

        # 可选：添加促销文字
        promotion = params.get("promotion", "")
        if promotion:
            processed = _add_promo_text(processed, promotion)

        out_path = run_dir / f"xhs_{idx + 1}.jpg"
        processed.save(str(out_path), "JPEG", quality=95)
        output_paths.append(str(out_path))
        log.info("Pillow 加工完成: %s", out_path)

    return output_paths


# ── 模式2：用户图片作为参考，AI 生成新图 ────────────────────

def _ai_from_reference(user_images: list, image_prompt: str, run_dir: Path) -> list[str]:
    """用 Seedream 图生图，用户图片作为参考"""
    if not image_prompt:
        image_prompt = "基于参考图生成精美的美食宣传图，保留食物主体，优化光线和构图，高清美食摄影"

    try:
        client = SeedreamClient()
        ref_paths = [Path(p) for p in user_images]
        paths = client.image_to_image(
            prompt=image_prompt,
            image_paths=ref_paths,
            output_dir=run_dir,
        )
        log.info("Seedream 图生图完成: %d 张", len(paths))
        return [str(p) for p in paths]
    except Exception as e:
        log.error("Seedream 图生图失败: %s", e)
        # 回退到 Pillow 加工
        log.info("回退到 Pillow 加工")
        return _pillow_edit(user_images, {}, run_dir)


# ── 模式3：无参考，AI 纯文生图 ─────────────────────────────

def _ai_generate(image_prompt: str, run_dir: Path, image_count: int = 1) -> list[str]:
    """用 Seedream 文生图，纯 AI 生成"""
    if not image_prompt:
        image_prompt = "台湾炸臭豆腐特写，金黄酥脆外皮，配蒜香酱油和酸菜，暖色调，高清美食摄影，4K"

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


# ── Pillow 工具函数 ────────────────────────────────────────

def _resize_and_pad(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """等比缩放并居中填充到目标尺寸，保持原图不变形"""
    tw, th = target_size
    ratio = min(tw / img.width, th / img.height)
    new_w = int(img.width * ratio)
    new_h = int(img.height * ratio)
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGB", (tw, th), (255, 255, 255))
    x = (tw - new_w) // 2
    y = (th - new_h) // 2
    canvas.paste(resized, (x, y))
    return canvas


def _add_promo_text(img: Image.Image, text: str) -> Image.Image:
    """在图片底部添加半透明促销文字条"""
    img = img.copy()
    w, h = img.size

    try:
        font = ImageFont.truetype(_FONT_PATH, size=max(28, h // 20))
    except Exception:
        font = ImageFont.load_default()

    # 底部半透明黑色条
    bar_h = h // 7
    bar_y = h - bar_h

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle([(0, bar_y), (w, h)], fill=(0, 0, 0, 140))
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    img = img.convert("RGB")

    # 居中文字
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = (w - text_w) // 2
    text_y = bar_y + (bar_h - (bbox[3] - bbox[1])) // 2
    draw.text((text_x, text_y), text, fill=(255, 255, 255), font=font)

    return img
