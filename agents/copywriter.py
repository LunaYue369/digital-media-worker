"""文案撰写 Agent — 为商家生成小红书宣传文案

包含标题 + 正文 + hashtag 的完整小红书帖子。
支持首次撰写和根据审核反馈重写。
"""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from core.merchant_config import store_name as _store_name
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
        copy_dict: {"title": "标题", "content": "正文", "tags": ["#标签1", "#标签2"]}
        usage_dict: {prompt: int, completion: int, cost: float}
    """
    system_prompt = build_system_prompt("copywriter")
    gpt_client = _get_client()

    json_format_instruction = (
        '请以 JSON 格式回复：\n'
        '{"title": "标题文字", "content": "正文内容（包含 emoji 和分段）", '
        '"tags": ["#标签1", "#标签2", "#标签3"]}\n'
        '注意：title 是纯标题，不含 emoji 和标签；content 是正文部分；tags 是标签数组，每个以 # 开头。'
    )

    # 构建 user prompt
    if feedback and previous_copy:
        prev_title = previous_copy.get("title", "")
        prev_content = previous_copy.get("content", "")
        prev_tags = " ".join(previous_copy.get("tags", []))
        prev_text = f"标题：{prev_title}\n\n正文：{prev_content}\n\n标签：{prev_tags}"
        user_msg = (
            f"请根据审核反馈重写小红书文案。用中文撰写。\n\n"
            f"产品：{params.get('product', '')}，{params.get('promotion', '')}\n"
            f"截止日期：{params.get('deadline', '无')}\n"
            f"风格：{params.get('style', '无特殊偏好')}\n"
            f"特殊要求：{params.get('extra_requests', '无')}\n\n"
            f"审核反馈：\n{feedback}\n\n"
            f"上一版文案：\n{prev_text}\n\n"
            f"请修正问题，保留好的部分。\n\n"
            f"{json_format_instruction}"
        )
    else:
        user_msg = (
            f"为{_store_name()}撰写一篇小红书文案。用中文撰写。\n\n"
            f"产品：{params.get('product', '')}，{params.get('promotion', '')}\n"
            f"截止日期：{params.get('deadline', '无')}\n"
            f"风格：{params.get('style', '无特殊偏好')}\n"
            f"特殊要求：{params.get('extra_requests', '无')}\n\n"
            f"请生成一篇完整的小红书文案，包含：\n"
            f"1. 吸引眼球的标题（有 hook）\n"
            f"2. 正文（300-500 字，分段，适量 emoji）\n"
            f"3. 5-8 个话题标签\n\n"
            f"{json_format_instruction}"
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
        # fallback: 把整段文本当正文
        copy_dict = {"title": "", "content": resp.choices[0].message.content, "tags": []}

    # 兼容旧格式：如果返回了 "post" 而非 title/content/tags，自动拆分
    if "post" in copy_dict and "title" not in copy_dict:
        copy_dict = _split_post_text(copy_dict["post"])

    # 确保 tags 格式正确
    tags = copy_dict.get("tags", [])
    if isinstance(tags, str):
        # "tags" 可能是字符串 "#tag1 #tag2"，拆成列表
        tags = [t.strip() for t in tags.split("#") if t.strip()]
        tags = [f"#{t}" for t in tags]
        copy_dict["tags"] = tags

    usage = {"prompt": pt, "completion": ct, "cost": cost}
    return copy_dict, usage


def _split_post_text(post: str) -> dict:
    """兼容旧格式：把 '标题\\n\\n正文\\n\\n#tag1 #tag2' 拆分成结构化 dict"""
    lines = post.strip().split("\n")
    title = lines[0].strip() if lines else ""

    # 提取 tags（最后连续的 # 开头行）
    tags = []
    content_lines = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("#") and all(
            part.startswith("#") for part in stripped.split() if part
        ):
            tags.extend(t.strip() for t in stripped.split() if t.strip())
        else:
            content_lines.append(line)

    content = "\n".join(content_lines).strip()
    return {"title": title, "content": content, "tags": tags}
