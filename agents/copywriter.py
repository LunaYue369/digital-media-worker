"""文案撰写 Agent — 为 Tofu King 生成小红书宣传文案

包含标题 + 正文 + hashtag 的完整小红书帖子。
支持首次撰写和根据审核反馈重写。
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


def write_copy(params: dict, knowledge: dict, session_id: str,
               feedback: str = "", previous_copy: dict | None = None) -> tuple[dict, dict]:
    """生成/重写小红书文案

    Args:
        params: 生成参数 {product, promotion, deadline, style, extra_requests}
        knowledge: 店铺知识（已内置在 soul 中，这里传额外信息）
        session_id: 会话 ID（用于记录 token）
        feedback: 审核反馈（重写时使用）
        previous_copy: 上一版文案（重写时使用）

    Returns:
        (copy_dict, usage_dict)
        copy_dict: {"小红书": "标题+正文+hashtag"}
        usage_dict: {prompt: int, completion: int, cost: float}
    """
    system_prompt = build_system_prompt("copywriter")
    gpt_client = _get_client()

    # 构建 user prompt
    if feedback and previous_copy:
        prev_text = previous_copy.get("小红书", "")
        user_msg = (
            f"请根据审核反馈重写小红书文案。\n\n"
            f"宣传内容：{params.get('product', '')}，{params.get('promotion', '')}\n"
            f"截止日期：{params.get('deadline', '无')}\n"
            f"风格偏好：{params.get('style', '无特定要求')}\n"
            f"特殊要求：{params.get('extra_requests', '无')}\n\n"
            f"审核反馈：\n{feedback}\n\n"
            f"上一版文案：\n{prev_text}\n\n"
            f"请针对反馈修改，保留好的部分，修正问题。\n\n"
            f"请以 JSON 格式回复：{{\"小红书\": \"标题\\n\\n正文\\n\\n#hashtag1 #hashtag2 ...\"}}"
        )
    else:
        user_msg = (
            f"请为 Tofu King 撰写一篇小红书帖子。\n\n"
            f"宣传内容：{params.get('product', '')}，{params.get('promotion', '')}\n"
            f"截止日期：{params.get('deadline', '无')}\n"
            f"风格偏好：{params.get('style', '无特定要求')}\n"
            f"特殊要求：{params.get('extra_requests', '无')}\n\n"
            f"请生成完整的小红书帖子，包含：\n"
            f"1. 一个吸人眼球的标题（15-25字，有 hook）\n"
            f"2. 正文（300-500字，分段，适量 emoji）\n"
            f"3. 5-8 个 hashtag\n\n"
            f"请以 JSON 格式回复：{{\"小红书\": \"标题\\n\\n正文\\n\\n#hashtag1 #hashtag2 ...\"}}"
        )

    resp = gpt_client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.6,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    cost = estimate_cost(pt, ct)
    record_usage(session_id, "copywriter", pt, ct)

    # 解析文案
    try:
        copy_dict = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("文案 JSON 解析失败: %s", resp.choices[0].message.content[:200])
        copy_dict = {"小红书": resp.choices[0].message.content}

    usage = {"prompt": pt, "completion": ct, "cost": cost}
    return copy_dict, usage
