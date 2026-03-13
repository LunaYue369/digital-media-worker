"""商家配置加载器 — 根据 MERCHANT 环境变量加载对应商家的配置

启动时通过 MERCHANT 环境变量指定商家：
    MERCHANT=tofu_king python main.py
    MERCHANT=boba_shop python main.py

配置来源：
    merchants/{merchant_id}/merchant.json  — 店铺基本信息
    merchants/{merchant_id}/souls/         — 人格文件目录
    merchants/{merchant_id}/.env           — 商家专属环境变量（Slack Token 等）
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MERCHANTS_DIR = _PROJECT_ROOT / "merchants"

# 当前商家配置（启动时加载）
_config: dict = {}
_merchant_id: str = ""


def get_merchant_dir() -> Path:
    """获取当前商家的目录路径"""
    return _MERCHANTS_DIR / _merchant_id


def get_souls_dir() -> Path:
    """获取当前商家的 souls 目录路径"""
    return get_merchant_dir() / "souls"


def load_merchant_config() -> dict:
    """加载当前商家配置（由 MERCHANT 环境变量指定）

    Returns:
        merchant.json 中的配置字典
    """
    global _config, _merchant_id

    _merchant_id = os.environ.get("MERCHANT", "")
    available = [d.name for d in _MERCHANTS_DIR.iterdir() if d.is_dir()] if _MERCHANTS_DIR.is_dir() else []

    if not _merchant_id:
        log.error("未设置 MERCHANT 环境变量！")
        log.error("可用商家: %s", available)
        raise SystemExit(
            "\n╔══════════════════════════════════════════════════╗\n"
            "║  请指定 MERCHANT 环境变量再启动：               ║\n"
            "║                                                  ║\n"
            + "".join(f"║    MERCHANT={m:<20s} python main.py  ║\n" for m in available) +
            "║                                                  ║\n"
            "╚══════════════════════════════════════════════════╝"
        )

    merchant_dir = _MERCHANTS_DIR / _merchant_id

    if not merchant_dir.is_dir():
        log.error("商家目录不存在: %s", merchant_dir)
        log.error("可用商家: %s", available)
        raise SystemExit(f"商家 '{_merchant_id}' 不存在。可用: {available}")

    # 加载 merchant.json
    config_path = merchant_dir / "merchant.json"
    if not config_path.exists():
        raise FileNotFoundError(f"商家配置不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        _config = json.load(f)

    log.info("已加载商家配置: %s (%s)", _config.get("store_name"), _merchant_id)

    # 加载商家专属 .env（如果存在）
    merchant_env = merchant_dir / ".env"
    if merchant_env.exists():
        from dotenv import load_dotenv
        load_dotenv(merchant_env, override=True)
        log.info("已加载商家环境变量: %s", merchant_env)

    return _config


def get_config() -> dict:
    """获取当前商家配置"""
    if not _config:
        load_merchant_config()
    return _config


def get(key: str, default=None):
    """获取配置项"""
    return get_config().get(key, default)


def store_name() -> str:
    """获取店铺全名"""
    return get("store_name", "未配置店铺")


def store_name_short() -> str:
    """获取店铺简称"""
    return get("store_name_short", store_name())


def default_product() -> str:
    """获取默认产品名"""
    return get("default_product", "招牌产品")


def fallback_image_prompt() -> str:
    """获取兜底图片提示词"""
    return get("fallback_image_prompt", "美食特写，暖色调，高清摄影，4K")


def fallback_video_prompt() -> str:
    """获取兜底视频提示词"""
    return get("fallback_video_prompt", "写实风格，美食特写，镜头缓慢推近，暖色调，美食广告质感")
