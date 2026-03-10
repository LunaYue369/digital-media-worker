"""Slack 图片下载器 — 下载用户在 Slack 中上传的图片到本地"""

import logging
import os
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# 用户上传图片的本地存储目录
UPLOAD_DIR = Path.home() / "Desktop" / "media" / "uploads"


def download_slack_files(url: str, filename: str, token: str) -> str | None:
    """从 Slack 下载文件到本地

    Args:
        url: Slack 文件的 url_private
        filename: 原始文件名
        token: Slack Bot Token（用于认证）

    Returns:
        本地文件路径，失败返回 None
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 加时间戳避免文件名冲突
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = f"{ts}_{filename}"
    local_path = UPLOAD_DIR / local_name

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        resp.raise_for_status()

        with open(local_path, "wb") as f:
            f.write(resp.content)

        log.info("已下载 Slack 文件: %s → %s", filename, local_path)
        return str(local_path)

    except Exception as e:
        log.error("下载 Slack 文件失败 %s: %s", filename, e)
        return None
