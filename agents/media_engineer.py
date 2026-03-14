"""媒体提示词专家 — 把用户的简单需求翻译成高质量的 Seedream/Seedance 提示词

调用 GPT 生成优化后的图片/视频提示词，供 image_processor 和 video_generator 使用。
"""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from core.merchant_config import store_name_short as _store_name_short, default_product as _default_product
from services.usage_tracker import record_usage, estimate_cost

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-4.1-mini")
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=3)
    return _client


def generate_prompts(params: dict, session_id: str,
                     need_enhance: bool = False, need_reference: bool = False,
                     need_generate: bool = False, need_video: bool = False,
                     has_reference_images: bool = False,
                     # 向后兼容旧调用方式
                     need_image: bool = False) -> dict:
    """生成优化后的图片/视频提示词

    Args:
        params: 用户需求参数 {product, promotion, style, extra_requests}
        session_id: 会话 ID
        need_enhance: 是否需要美化提示词（保留原图主体，加字/滤镜/调色）
        need_reference: 是否需要参考生成提示词（以原图为灵感，生成全新图）
        need_generate: 是否需要纯文生图提示词（无参考图）
        need_video: 是否需要视频提示词
        has_reference_images: 是否有参考图（向后兼容，影响视频提示词）
        need_image: 向后兼容旧调用（等同于 need_generate=True 或 need_reference=True）

    Returns:
        {"enhance_prompt": "...", "reference_prompt": "...", "image_prompt": "...", "video_prompt": "..."}
    """
    # 向后兼容：旧代码传 need_image=True 时自动映射
    if need_image and not (need_enhance or need_reference or need_generate):
        if has_reference_images:
            need_reference = True
        else:
            need_generate = True
    system_prompt = build_system_prompt("media_engineer")
    gpt_client = _get_client()

    # 构建需求描述
    parts = [f"请为{_store_name_short()}生成媒体提示词。\n"]
    parts.append(f"宣传内容：{params.get('product', _default_product())}")
    if params.get("promotion"):
        parts.append(f"促销信息：{params['promotion']}")
    if params.get("style"):
        parts.append(f"文案风格偏好：{params['style']}")
    if params.get("extra_requests"):
        parts.append(f"特殊要求：{params['extra_requests']}")

    # 图片细节参数
    if need_image:
        img_details = []
        if params.get("image_style"):
            img_details.append(f"图片风格：{params['image_style']}")
        if params.get("image_composition"):
            img_details.append(f"构图：{params['image_composition']}")
        if params.get("image_lighting"):
            img_details.append(f"光线：{params['image_lighting']}")
        if params.get("image_color_tone"):
            img_details.append(f"色调：{params['image_color_tone']}")
        if params.get("image_extra"):
            img_details.append(f"特殊要求：{params['image_extra']}")
        if img_details:
            parts.append(f"\n图片细节要求：{'，'.join(img_details)}")

    # 视频细节参数
    if need_video:
        vid_details = []
        if params.get("video_camera"):
            vid_details.append(f"运镜：{params['video_camera']}")
        if params.get("video_sound"):
            vid_details.append(f"音效/BGM：{params['video_sound']}")
        if params.get("video_style"):
            vid_details.append(f"视频风格：{params['video_style']}")
        if params.get("video_scene"):
            vid_details.append(f"画面场景：{params['video_scene']}")
        if vid_details:
            parts.append(f"\n视频细节要求：{'，'.join(vid_details)}")

    # 说明需要什么
    needs = []
    if need_enhance:
        needs.append(
            "enhance_prompt（美化提示词：用户已有原图，需要在保留原图主体和构图的基础上做美化处理，"
            "如添加宣传文字、品牌 logo、滤镜效果、调色、光线增强等。提示词应强调「保留原图」+「叠加效果」）"
        )
    if need_reference:
        needs.append(
            "reference_prompt（参考生成提示词：用户已有参考图，需要以参考图的内容/构图/风格为灵感，"
            "生成一张全新的宣传图片。提示词应描述新图的完整场景，可以大幅偏离原图）"
        )
    if need_generate:
        needs.append(
            "image_prompt（纯文生图提示词：无参考图，请写详细的场景描述，"
            "包括食物、摆盘、背景、光线、氛围等细节）"
        )
    if need_video:
        if has_reference_images:
            needs.append("video_prompt（视频提示词：用户已有参考图作为首帧，请描述动态效果和运镜）")
        else:
            needs.append("video_prompt（纯文生视频提示词：请描述完整场景和动态）")

    parts.append(f"\n需要生成：{'; '.join(needs)}")

    # 构建 JSON 回复格式
    json_fields = []
    if need_enhance:
        json_fields.append('"enhance_prompt": "..."')
    if need_reference:
        json_fields.append('"reference_prompt": "..."')
    if need_generate:
        json_fields.append('"image_prompt": "..."')
    if need_video:
        json_fields.append('"video_prompt": "..."')
    parts.append(f"\n请以 JSON 格式回复：{{{', '.join(json_fields)}}}")
    parts.append("不需要的字段留空字符串即可。")

    user_msg = "\n".join(parts)

    resp = gpt_client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.5,
        max_tokens=600,
        response_format={"type": "json_object"},
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    cost = estimate_cost(pt, ct)
    record_usage(session_id, "media_engineer", pt, ct)

    try:
        result = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("媒体工程师 JSON 解析失败: %s", resp.choices[0].message.content[:200])
        result = {"image_prompt": "", "video_prompt": ""}

    log.info("生成媒体提示词: image=%d字 video=%d字",
             len(result.get("image_prompt", "")),
             len(result.get("video_prompt", "")))

    return result, {"prompt": pt, "completion": ct, "cost": cost}
