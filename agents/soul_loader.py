"""人格加载器 — 从 souls/ 目录加载 agent 人格 markdown 文件

启动时一次性加载所有人格：
  _shared.md  → 所有 agent 共用的背景知识
  copywriter.md → 文案撰写人格
  reviewer.md   → 文案审核人格
  assistant.md  → 对话助手人格
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOULS_DIR = Path(os.path.dirname(__file__)) / "souls"

# 储存各人格内容
_souls: dict[str, str] = {}
# 通用背景知识
_shared: str = ""


def load_all():
    """启动时加载所有人格文件"""
    global _shared

    if not SOULS_DIR.is_dir():
        log.error("人格目录不存在: %s", SOULS_DIR)
        return

    # 加载 _shared.md
    shared_path = SOULS_DIR / "_shared.md"
    if shared_path.exists():
        _shared = shared_path.read_text(encoding="utf-8")
        log.info("已加载通用人格 (%d 字符)", len(_shared))

    # 加载各独立人格
    for md_file in SOULS_DIR.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        agent_id = md_file.stem
        _souls[agent_id] = md_file.read_text(encoding="utf-8")
        log.info("已加载人格: %s (%d 字符)", agent_id, len(_souls[agent_id]))

    if not _souls:
        log.warning("未加载任何人格 — 请检查 %s", SOULS_DIR)
    else:
        log.info("人格加载完成 — 共 %d 个", len(_souls))


def get_shared() -> str:
    return _shared


def get_soul(agent_id: str) -> str:
    return _souls.get(agent_id, "")


def build_system_prompt(agent_id: str) -> str:
    """拼接通用背景 + 独立人格 → 完整 system prompt"""
    parts = []
    if _shared:
        parts.append(_shared)
    soul = get_soul(agent_id)
    if soul:
        parts.append(soul)
    return "\n\n---\n\n".join(parts)
