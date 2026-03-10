"""会话状态管理 — 每个 Slack thread 对应一个独立会话

状态机：
  gathering_info → generating → reviewing → done
       ↑                                    │
       └────────────────────────────────────┘  (用户要求修改时回到 gathering_info)

每个会话存储：对话历史、店铺知识、当前草稿、用户上传的图片、token 用量
"""

import threading
import time

# 线程安全锁
_lock = threading.Lock()

# 所有活跃会话，key = thread_ts
_sessions: dict[str, dict] = {}

# 会话状态常量
GATHERING = "gathering_info"
GENERATING = "generating"
REVIEWING = "reviewing"
DONE = "done"


def get_or_create(thread_ts: str, channel: str) -> dict:
    """获取或创建一个会话，thread_ts 作为唯一标识"""
    with _lock:
        if thread_ts not in _sessions:
            _sessions[thread_ts] = {
                "thread_ts": thread_ts,
                "channel": channel,
                "stage": GATHERING,
                "messages": [],           # 完整对话历史（给 GPT 用）
                "params": {},             # 提取出的生成参数（产品、活动、风格等）
                "platforms": ["小红书"],   # 目标平台（固定小红书）
                "user_images": [],        # 用户上传的图片本地路径
                "draft": {},              # 当前草稿 {copy: {}, images: [], video: ""}
                "usage": {                # 本次会话 token 用量
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "api_calls": 0,
                    "estimated_cost": 0.0,
                },
                "created_at": time.time(),
            }
        return _sessions[thread_ts]


def get(thread_ts: str) -> dict | None:
    """获取已有会话，不存在则返回 None"""
    with _lock:
        return _sessions.get(thread_ts)


def update_stage(thread_ts: str, stage: str):
    """更新会话状态"""
    with _lock:
        if thread_ts in _sessions:
            _sessions[thread_ts]["stage"] = stage


def add_message(thread_ts: str, role: str, content: str):
    """添加一条对话记录"""
    with _lock:
        if thread_ts in _sessions:
            _sessions[thread_ts]["messages"].append({
                "role": role,
                "content": content,
            })


def add_user_image(thread_ts: str, path: str):
    """记录用户上传的图片路径（自动去重）"""
    with _lock:
        if thread_ts in _sessions:
            if path not in _sessions[thread_ts]["user_images"]:
                _sessions[thread_ts]["user_images"].append(path)


def add_usage(thread_ts: str, prompt_tokens: int, completion_tokens: int, cost: float):
    """累加 token 用量"""
    with _lock:
        if thread_ts in _sessions:
            u = _sessions[thread_ts]["usage"]
            u["prompt_tokens"] += prompt_tokens
            u["completion_tokens"] += completion_tokens
            u["api_calls"] += 1
            u["estimated_cost"] = round(u["estimated_cost"] + cost, 6)


def cleanup_old(max_age_hours: int = 24):
    """清理超时的会话，释放内存"""
    cutoff = time.time() - max_age_hours * 3600
    with _lock:
        expired = [ts for ts, s in _sessions.items() if s["created_at"] < cutoff]
        for ts in expired:
            del _sessions[ts]
    return len(expired)
