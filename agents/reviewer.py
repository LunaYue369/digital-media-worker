"""文案审核 Agent — 对生成的文案进行质量审核，最多 3 轮自动重写

审核维度（每项 1-10 分）：
- 吸引力：标题/开头是否抓眼球
- 准确性：促销信息是否准确（价格、日期、产品名）
- 平台适配：是否符合目标平台的风格
- 合规性：有无违规/夸大宣传内容
- 感染力：是否有让人想去消费的冲动

通过条件：所有维度 >= 6 分
"""

import json
import logging
import os

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from services.usage_tracker import record_usage, estimate_cost

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-4.1-mini")
MAX_ROUNDS = int(os.getenv("REVIEWER_MAX_ROUNDS", "3"))
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=3)
    return _client


def review_copy(copy_dict: dict, params: dict, session_id: str) -> dict:
    """审核文案，返回审核结果

    Args:
        copy_dict: {"title": "标题", "content": "正文", "tags": ["#tag1", ...]}
        params: 生成参数（用于验证准确性）
        session_id: 会话 ID

    Returns:
        {
            "approved": bool,
            "scores": {维度: 分数},
            "issues": [问题列表],
            "suggestions": [建议列表],
            "verdict": "总评"
        }
    """
    system_prompt = build_system_prompt("reviewer")
    gpt_client = _get_client()

    # 拼接文案供审核
    title = copy_dict.get("title", "")
    content = copy_dict.get("content", "")
    tags = " ".join(copy_dict.get("tags", []))
    copy_text = f"【标题】\n{title}\n\n【正文】\n{content}\n\n【标签】\n{tags}"

    user_msg = (
        f"请审核以下宣传文案。\n\n"
        f"宣传内容：{params.get('product', '')}，{params.get('promotion', '')}\n"
        f"截止日期：{params.get('deadline', '无')}\n"
        f"特殊要求：{params.get('extra_requests', '无')}\n\n"
        f"待审核文案：\n{copy_text}\n\n"
        f"请以 JSON 格式回复：\n"
        f'{{"approved": true/false, "scores": {{"吸引力": 8, "准确性": 9, "平台适配": 7, "合规性": 9, "感染力": 8}}, '
        f'"issues": ["问题1"], "suggestions": ["建议1"], "verdict": "总评"}}'
    )

    resp = gpt_client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=600,
        response_format={"type": "json_object"},
    )

    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    record_usage(session_id, "reviewer", pt, ct)

    try:
        result = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        log.error("审核 JSON 解析失败: %s", resp.choices[0].message.content[:200])
        result = {"approved": False, "scores": {}, "issues": ["JSON 解析失败"], "suggestions": [], "verdict": "无法解析"}

    log.info("文案审核: approved=%s scores=%s", result.get("approved"), result.get("scores"))
    return result


def build_feedback(review: dict) -> str:
    """把审核结果格式化为反馈字符串，供 copywriter 重写时参考"""
    parts = []
    if review.get("issues"):
        parts.append("问题：\n" + "\n".join(f"- {i}" for i in review["issues"]))
    if review.get("suggestions"):
        parts.append("建议：\n" + "\n".join(f"- {s}" for s in review["suggestions"]))
    if review.get("verdict"):
        parts.append(f"总评：{review['verdict']}")
    return "\n\n".join(parts)


def get_max_rounds() -> int:
    return MAX_ROUNDS
