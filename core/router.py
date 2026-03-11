"""消息路由 — 接收 Slack 消息，分发到对话层或执行层

职责：
1. 过滤 bot 自身消息，避免死循环
2. 下载用户上传的图片
3. 根据会话状态决定走对话层还是执行层
4. 处理按钮交互（满意/重新生成/修改意见）
"""

import logging
import os
import re
import threading

from core import session
from core.session import GATHERING, GENERATING, REVIEWING, DONE
from agents.conversation import chat_and_maybe_generate
from pipeline.promo_pipeline import run_pipeline, publish_draft_to_xhs
from services.image_downloader import download_slack_files
from slack_ui.blocks import build_approved_message

log = logging.getLogger(__name__)


def handle_message(event: dict, say, client):
    """处理所有用户消息的入口"""
    # 过滤 bot 自身消息
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    # 调试：打印事件中的 files 字段，排查图片上传问题
    log.info("收到事件 type=%s, subtype=%s, files=%s, channel_type=%s",
             event.get("type"), event.get("subtype"),
             [f.get("name") for f in event.get("files", [])],
             event.get("channel_type"))

    text = (event.get("text") or "").strip()
    # 去掉 @mention 前缀
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

    if not text and not event.get("files"):
        return

    # 确定 thread_ts（在 thread 内回复则用原 thread，否则用消息本身的 ts）
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel = event.get("channel")

    # 获取或创建会话
    sess = session.get_or_create(thread_ts, channel)

    # 下载用户上传的图片
    # app_mention 事件可能不包含 files 字段，需要通过 API 补充获取
    files = event.get("files", [])
    if not files:
        files = _fetch_files_from_event(client, channel, event.get("ts"))
    if files:
        _download_images_async(files, thread_ts, client, say)

    # 记录用户消息到会话历史
    if text:
        session.add_message(thread_ts, "user", text)

    stage = sess["stage"]

    # 根据状态分发
    if stage == GATHERING:
        # 对话层：理解用户意图，收集信息，判断是否可以开始生成
        threading.Thread(
            target=_safe_run,
            args=(chat_and_maybe_generate, sess, text, say, client),
            daemon=True,
        ).start()

    elif stage == GENERATING:
        # 正在生成中，告知用户等待
        say(text="正在为您生成中，请稍等...", thread_ts=thread_ts)

    elif stage == REVIEWING:
        # 审核阶段，等待用户点击按钮（满意/重新生成）
        say(text="请点击上方按钮选择：满意 或 重新生成。", thread_ts=thread_ts)

    elif stage == DONE:
        # 已完成，如果用户继续说话，开启新一轮
        session.update_stage(thread_ts, GATHERING)
        session.add_message(thread_ts, "user", text)
        threading.Thread(
            target=_safe_run,
            args=(chat_and_maybe_generate, sess, text, say, client),
            daemon=True,
        ).start()


def handle_action(action_type: str, body: dict, say, client):
    """处理按钮交互"""
    # 从 body 中取出 thread_ts
    message = body.get("message", {})
    thread_ts = message.get("thread_ts") or message.get("ts")
    channel = body.get("channel", {}).get("id")

    if not thread_ts:
        return

    sess = session.get(thread_ts)
    if not sess:
        say(text="会话已过期，请重新开始。", thread_ts=thread_ts)
        return

    if action_type == "approve":
        # ── 用户点击「满意」──
        # 标记会话为已完成，并发送带「发布到小红书」按钮的确认消息
        # 用户可以选择点击按钮自动发布，也可以忽略按钮自行手动发布
        session.update_stage(thread_ts, DONE)
        blocks = build_approved_message(sess["usage"])
        say(blocks=blocks, text="素材已确认", thread_ts=thread_ts)

    elif action_type == "regenerate":
        # ── 用户点击「重新生成」──
        # 回到生成状态，重新执行完整 pipeline
        session.update_stage(thread_ts, GENERATING)
        say(text="好的，正在重新生成全部素材...", thread_ts=thread_ts)
        threading.Thread(
            target=_safe_run,
            args=(run_pipeline, sess, say, client),
            daemon=True,
        ).start()

    elif action_type == "publish_to_xhs":
        # ── 用户点击「发布到小红书」──
        # 在后台线程中执行发布，避免阻塞 Slack 的 3 秒 ack 超时
        # publish_draft_to_xhs 内部会通过 say() 回报发布结果
        threading.Thread(
            target=_safe_run,
            args=(publish_draft_to_xhs, sess, say),
            daemon=True,
        ).start()



def _fetch_files_from_event(client, channel: str, ts: str) -> list:
    """当事件中没有 files 时，通过 Slack API 获取该消息的附件

    app_mention 事件通常不含 files 字段，需要用 conversations.history
    或 conversations.replies 来获取用户上传的图片。
    """
    if not ts:
        return []
    try:
        # 先尝试作为 thread reply 获取
        resp = client.conversations_replies(
            channel=channel, ts=ts, limit=1, inclusive=True
        )
        messages = resp.get("messages", [])
        for msg in messages:
            if msg.get("ts") == ts and msg.get("files"):
                log.info("通过 API 补充获取到 %d 个文件", len(msg["files"]))
                return msg["files"]
    except Exception as e:
        log.warning("获取消息附件失败: %s", e)
    return []


def download_images_for_thread(files: list, thread_ts: str, client):
    """下载图片并关联到会话（公开方法，供 main.py 的 message 事件补充调用）"""
    _download_images_async(files, thread_ts, client, None)


def _download_images_async(files: list, thread_ts: str, client, say):
    """异步下载用户上传的图片"""
    token = os.environ["SLACK_BOT_TOKEN"]
    for f in files:
        if f.get("mimetype", "").startswith("image/"):
            path = download_slack_files(f["url_private"], f["name"], token)
            if path:
                session.add_user_image(thread_ts, path)
                log.info("已下载用户图片: %s → %s", f["name"], path)


def _safe_run(func, *args):
    """安全运行函数，捕获异常并记录日志"""
    try:
        func(*args)
    except Exception:
        log.exception("Pipeline/对话层执行出错")
