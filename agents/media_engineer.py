"""媒体提示词专家 — 把用户的简单需求翻译成高质量的 Seedream/Seedance 提示词

调用 GPT 生成优化后的图片/视频提示词，供 image_processor 和 video_generator 使用。
"""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
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
                     need_image: bool = True, need_video: bool = False,
                     has_reference_images: bool = False) -> dict:
    """生成优化后的图片/视频提示词

    Args:
        params: 用户需求参数 {product, promotion, style, extra_requests}
        session_id: 会话 ID
        need_image: 是否需要图片提示词
        need_video: 是否需要视频提示词
        has_reference_images: 是否有参考图（影响提示词写法）

    Returns:
        {"image_prompt": "...", "video_prompt": "..."}
    """
    system_prompt = build_system_prompt("media_engineer")
    gpt_client = _get_client()

    # 构建需求描述
    parts = [f"请为 Tofu King 生成媒体提示词。\n"]
    parts.append(f"宣传内容：{params.get('product', '招牌臭豆腐')}")
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
    if need_image:
        if has_reference_images:
            needs.append("图片提示词（用户已有参考图，请写「图生图」风格的提示词，描述如何基于参考图做变换/增强）")
        else:
            needs.append("图片提示词（纯文生图，请写详细的场景描述）")
    if need_video:
        if has_reference_images:
            needs.append("视频提示词（用户已有参考图作为首帧，请描述动态效果和运镜）")
        else:
            needs.append("视频提示词（纯文生视频，请描述完整场景和动态）")

    parts.append(f"\n需要生成：{'; '.join(needs)}")
    parts.append("\n请以 JSON 格式回复：{\"image_prompt\": \"...\", \"video_prompt\": \"...\"}")
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
