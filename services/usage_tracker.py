"""Token 用量追踪 — 线程安全，支持按会话/步骤统计

记录每一次 GPT API 调用的 token 消耗和预估费用，
可以计算生成一篇完整 Post 的总成本。
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "usage_log.json")
_lock = threading.Lock()

# GPT-4.1-mini 定价（每 1K tokens）
_COST_PER_1K = {
    "prompt": 0.0004,       # $0.40/M 输入
    "completion": 0.0016,    # $1.60/M 输出
}

# 各步骤分类
_STEP_CATEGORIES = {
    "conversation": "对话理解",
    "copywriter": "文案撰写",
    "reviewer": "文案审核",
    "media_engineer": "媒体提示词",
}


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """计算单次 API 调用的预估费用"""
    return round(
        prompt_tokens / 1000 * _COST_PER_1K["prompt"]
        + completion_tokens / 1000 * _COST_PER_1K["completion"],
        6,
    )


def record_usage(session_id: str, step: str, prompt_tokens: int, completion_tokens: int):
    """记录一次 GPT API 调用，线程安全"""
    cost = estimate_cost(prompt_tokens, completion_tokens)
    entry = {
        "session_id": session_id,
        "step": step,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost": cost,
        "timestamp": time.time(),
    }

    with _lock:
        data = _load()
        data["records"].append(entry)
        t = data["totals"]
        t["prompt_tokens"] += prompt_tokens
        t["completion_tokens"] += completion_tokens
        t["total_tokens"] += prompt_tokens + completion_tokens
        t["estimated_cost"] = round(t["estimated_cost"] + cost, 6)
        t["api_calls"] += 1
        _save(data)

    return entry


def get_session_summary(session_id: str) -> dict:
    """统计单个会话（一篇完整 Post）的 token 用量

    返回按步骤分类的明细 + 总计
    """
    with _lock:
        data = _load()

    entries = [e for e in data["records"] if e["session_id"] == session_id]
    if not entries:
        return {"session_id": session_id, "total_calls": 0}

    by_step: dict[str, dict] = {}
    total_prompt = total_completion = 0
    total_cost = 0.0

    for e in entries:
        step = e["step"]
        label = _STEP_CATEGORIES.get(step, step)
        if label not in by_step:
            by_step[label] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        by_step[label]["calls"] += 1
        by_step[label]["prompt_tokens"] += e["prompt_tokens"]
        by_step[label]["completion_tokens"] += e["completion_tokens"]
        by_step[label]["cost"] += e["estimated_cost"]
        total_prompt += e["prompt_tokens"]
        total_completion += e["completion_tokens"]
        total_cost += e["estimated_cost"]

    return {
        "session_id": session_id,
        "total_calls": len(entries),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cost": round(total_cost, 4),
        "by_step": by_step,
    }


def format_session_report(session_id: str) -> str:
    """格式化单个会话的用量报告（给 Slack 展示）"""
    s = get_session_summary(session_id)
    if s["total_calls"] == 0:
        return "本次无 API 调用记录。"

    lines = [
        "*本次生成用量统计*",
        f"API 调用次数：{s['total_calls']}",
        f"总 tokens：{s['total_tokens']:,}（输入 {s['total_prompt_tokens']:,} / 输出 {s['total_completion_tokens']:,}）",
        f"预估费用：${s['total_cost']:.4f}",
        "",
        "*各步骤明细：*",
    ]
    for step_name, info in s["by_step"].items():
        tokens = info["prompt_tokens"] + info["completion_tokens"]
        lines.append(
            f"  {step_name}：{info['calls']} 次调用，{tokens:,} tokens，${info['cost']:.4f}"
        )
    return "\n".join(lines)


def get_all_summary() -> dict:
    """获取全局累计统计"""
    with _lock:
        data = _load()
    return data["totals"]


# ── 内部方法 ──────────────────────────────────────────────────

def _empty_totals() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_cost": 0.0,
        "api_calls": 0,
    }


def _load() -> dict:
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"records": [], "totals": _empty_totals()}


def _save(data: dict):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
