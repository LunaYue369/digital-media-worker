"""人格加载器 — 从商家的 souls/ 目录加载 agent 人格 markdown 文件

启动时根据 MERCHANT 环境变量加载对应商家的人格文件：
  merchants/{merchant_id}/souls/_shared.md     → 所有 agent 共用的背景知识
  merchants/{merchant_id}/souls/copywriter.md  → 文案撰写人格
  merchants/{merchant_id}/souls/reviewer.md    → 文案审核人格
  merchants/{merchant_id}/souls/assistant.md   → 对话助手人格

向后兼容：如果商家目录不存在，回退到 agents/souls/ 目录。
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# 储存各人格内容
_souls: dict[str, str] = {}
# 通用背景知识
_shared: str = ""


def _get_souls_dir() -> Path:
    """获取当前商家的 souls 目录，不存在则回退到默认目录"""
    try:
        from core.merchant_config import get_souls_dir
        souls_dir = get_souls_dir()
        if souls_dir.is_dir():
            return souls_dir
    except Exception:
        pass

    # 回退到默认 agents/souls/
    fallback = Path(__file__).parent / "souls"
    log.warning("使用默认 souls 目录: %s", fallback)
    return fallback


def load_all():
    """启动时加载所有人格文件"""
    global _shared

    souls_dir = _get_souls_dir()

    if not souls_dir.is_dir():
        log.error("人格目录不存在: %s", souls_dir)
        return

    # 加载 _shared.md
    shared_path = souls_dir / "_shared.md"
    if shared_path.exists():
        _shared = shared_path.read_text(encoding="utf-8")
        log.info("已加载通用人格 (%d 字符) from %s", len(_shared), souls_dir)

    # 加载各独立人格
    for md_file in souls_dir.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        agent_id = md_file.stem
        _souls[agent_id] = md_file.read_text(encoding="utf-8")
        log.info("已加载人格: %s (%d 字符)", agent_id, len(_souls[agent_id]))

    if not _souls:
        log.warning("未加载任何人格 — 请检查 %s", souls_dir)
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
