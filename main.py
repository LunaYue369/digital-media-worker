"""DigitalMediaWorker — 小店宣传素材生成 Slack Bot (Socket Mode)

对话驱动的状态机 + Pipeline 执行引擎：
- 对话层：GPT 理解用户意图，提取参数
- 执行层：文案生成 → 审核循环 → 图片处理 → 视频生成
- 交互层：结果展示 + 按钮反馈 + 局部修改
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agents import soul_loader
from core.router import handle_message, handle_action

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# Slack App (Socket Mode)
app = App(token=os.environ["SLACK_BOT_TOKEN"])


# ── 事件监听 ──────────────────────────────────────────────────

@app.event("message")
def on_message(event, say, client):
    """消息处理 — DM 直接处理，频道内只补充下载图片（app_mention 事件可能不含 files）"""
    if event.get("channel_type") == "im":
        handle_message(event, say, client)
    elif event.get("files"):
        # 频道消息带附件 — 只下载图片，不重复处理文本/对话
        # （文本部分已经由 app_mention 事件处理）
        from core.router import download_images_for_thread
        thread_ts = event.get("thread_ts") or event.get("ts")
        download_images_for_thread(event.get("files", []), thread_ts, client)


@app.event("app_mention")
def on_mention(event, say, client):
    """频道 @mention 处理"""
    handle_message(event, say, client)


# ── 按钮交互 ──────────────────────────────────────────────────

@app.action("approve_draft")
def on_approve(ack, body, say, client):
    """用户点击「满意」"""
    ack()
    handle_action("approve", body, say, client)


@app.action("regenerate_draft")
def on_regenerate(ack, body, say, client):
    """用户点击「重新生成」"""
    ack()
    handle_action("regenerate", body, say, client)


@app.action("publish_to_xhs")
def on_publish_to_xhs(ack, body, say, client):
    """用户点击「发布到小红书」— 将已确认的草稿自动发布到小红书

    流程：用户先点「满意」确认草稿 → 再点此按钮触发发布
    发布在后台线程中执行，结果通过 say() 回报到 Slack thread
    """
    ack()
    handle_action("publish_to_xhs", body, say, client)


# ── 启动 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    soul_loader.load_all()
    log.info("DigitalMediaWorker 启动中 (Socket Mode)...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
