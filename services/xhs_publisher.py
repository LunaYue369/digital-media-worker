"""小红书自动发布服务 — 封装 XiaohongshuSkills CDP 发布逻辑

将 DigitalMediaWorker 生成的素材（文案 + 图片/视频）自动发布到小红书。
底层通过 Chrome DevTools Protocol (CDP) 操控 creator.xiaohongshu.com 创作者中心网页。

整体流程：
    1. 确保 Chrome 已启动并开启了远程调试端口
    2. 通过 WebSocket 连接 Chrome，检查小红书登录状态
    3. 如果未登录：headless 模式下自动切换到 headed 模式弹出浏览器让用户扫码
    4. 填写表单：上传图片/视频 → 输入标题 → 输入正文
    5. 在正文编辑器中逐字输入话题标签（模拟人类打字节奏，触发小红书的标签建议弹窗）
    6. 点击"发布"按钮完成发布

作为库调用（Bot 内部使用）：
    from services.xhs_publisher import publish_to_xhs, PublishConfig

    result = publish_to_xhs(
        title="今日推荐：臭豆腐大王",
        content="正文内容...",
        image_paths=["path/to/img1.jpg", "path/to/img2.jpg"],
        tags=["#美食", "#小吃"],
    )

作为独立脚本测试：
    python -m services.xhs_publisher --title "测试" --content "内容" --images img.jpg --preview
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ── 确保 xhs/ 子目录可被 import ──
# cdp_publish.py 等模块位于 services/xhs/ 目录下，需要加入 sys.path
_XHS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xhs")
if _XHS_DIR not in sys.path:
    sys.path.insert(0, _XHS_DIR)

from chrome_launcher import ensure_chrome, restart_chrome
from cdp_publish import XiaohongshuPublisher, CDPError
from run_lock import SingleInstanceError, single_instance
from account_manager import get_default_account

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class PublishResult:
    """发布结果

    Attributes:
        success: 是否发布成功
        status:  状态码，取值：
                 - PUBLISHED        — 已成功发布
                 - READY_TO_PUBLISH — 预览模式，表单已填好但未点发布
                 - NOT_LOGGED_IN    — 未登录，需要扫码
                 - ERROR            — 出错
        note_link: 发布成功后的笔记链接（可能为 None）
        error:     出错时的错误信息
    """
    success: bool
    status: str
    note_link: Optional[str] = None
    error: Optional[str] = None


@dataclass
class PublishConfig:
    """发布配置

    Attributes:
        host:             Chrome CDP 主机地址（默认 127.0.0.1 本机）
        port:             Chrome CDP 远程调试端口（默认 9222）
        headless:         是否用 headless 模式（无界面），自动发布推荐 True，
                          未登录时会自动切换到 headed 模式让用户扫码
        account:          小红书账号名称，None 则使用默认账号（通常是 "default"）
        preview:          预览模式 — True 时只填表单不点发布按钮，用于人工检查
        timing_jitter:    操作间的随机延迟比例（0~0.7），模拟人类操作节奏，防检测
        reuse_existing_tab: 是否复用已有的 Chrome 标签页（headed 模式下减少窗口切换）
        post_time:        定时发布时间字符串（格式 "YYYY-MM-DD HH:MM"），None 则立即发布
    """
    host: str = "127.0.0.1"
    port: int = 9222
    headless: bool = True
    account: Optional[str] = None
    preview: bool = False
    timing_jitter: float = 0.25
    reuse_existing_tab: bool = False
    post_time: Optional[str] = None


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _resolve_account(account: Optional[str]) -> str:
    """解析账号名称

    优先级：
    1. 显式传入的 account 参数
    2. accounts.json 中配置的默认账号
    3. 兜底返回 "default"

    单账号场景下（只有一个小红书号），不需要传 account 参数，
    会自动走 get_default_account() → 返回 "default"。
    """
    if account and account.strip():
        return account.strip()
    try:
        resolved = get_default_account()
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
    except Exception:
        pass
    return "default"


def _extract_tags_from_content(content: str) -> tuple[str, list[str]]:
    """从正文末尾提取话题标签（fallback 逻辑）

    当调用方没有显式传入 tags 参数时，尝试从正文的最后一行提取标签。
    要求最后一行的每个词都是 "#xxx" 格式，否则不提取。

    注意：这个函数只看最后一行。正常流程中 tags 已经从 copy_dict["tags"]
    单独传入了，这个函数主要服务于 CLI 直接调用的场景（用户把标签写在正文末尾）。

    Args:
        content: 笔记正文

    Returns:
        (去掉标签行的正文, 标签列表)
        如果最后一行不是标签，原样返回 (content, [])
    """
    import re
    lines = content.splitlines()
    # 跳过末尾的空行
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return content, []

    last_line = lines[-1].strip()
    parts = [p for p in last_line.split() if p]
    if not parts:
        return content, []

    # 每个词都必须是 "#xxx" 格式，否则认为最后一行不是标签行
    if not all(re.fullmatch(r"#[^\s#]+", part) for part in parts):
        return content, []

    body = "\n".join(lines[:-1]).strip()
    return body, parts


def _select_topics(
    publisher: XiaohongshuPublisher,
    tags: list[str],
    timing_jitter: float = 0.25,
):
    """在小红书编辑器中输入话题标签

    模拟人类在正文编辑器中逐字输入 "#标签名" 的过程：
    1. 将光标移到编辑器末尾
    2. 输入 "#" 符号，稍作停顿
    3. 逐字输入标签文字（每个字之间有随机延迟，模拟打字节奏）
    4. 等待小红书的标签建议弹窗出现（约 3 秒）
    5. 按回车确认选中建议的标签
    6. 插入空格分隔，继续下一个标签

    为什么要这样做？
    - 小红书的话题标签不是简单的文本，而是需要触发编辑器的标签建议功能
    - 直接粘贴 "#美食" 不会被识别为话题标签
    - 必须模拟逐字输入 + 等待建议 + 回车确认的完整流程

    Args:
        publisher: CDP 发布器实例（已连接）
        tags:      话题标签列表，如 ["#美食", "#推荐"]
        timing_jitter: 随机延迟比例，越大越随机（防检测）
    """
    if not tags:
        return

    import json
    import random

    log.info("正在输入 %d 个话题标签...", len(tags))

    for index, tag in enumerate(tags):
        # 去掉 "#" 前缀（后面会手动输入 "#"）
        normalized = tag.lstrip("#").strip()
        if not normalized:
            continue

        # ── 计算各步骤的随机延迟（毫秒） ──
        def _jitter_ms(base, ratio, min_ms=0):
            """在 base 值附近随机浮动，模拟人类不规则的操作节奏"""
            base = max(min_ms, int(base))
            if ratio <= 0:
                return base
            delta = int(round(base * ratio))
            return random.randint(max(min_ms, base - delta), max(base - delta, base + delta))

        hash_pause = _jitter_ms(180, timing_jitter, 90)       # 输入 "#" 后的停顿
        char_min = _jitter_ms(45, timing_jitter, 25)           # 每个字的最小间隔
        char_max = _jitter_ms(95, timing_jitter, char_min)     # 每个字的最大间隔
        suggest_wait = _jitter_ms(3000, timing_jitter, 1600)   # 等待建议弹窗的时间
        after_enter = _jitter_ms(260, timing_jitter, 120)      # 回车确认后的停顿

        # 将字符串转为 JS 安全的字面量
        escaped_tag = json.dumps(normalized)
        newline_lit = json.dumps("\n")
        hash_lit = json.dumps("#")
        space_lit = json.dumps(" ")

        # ── 在浏览器中执行 JS：模拟逐字输入标签 ──
        result = publisher._evaluate(f"""
            (async function() {{
                // 找到小红书的正文编辑器（TipTap / ProseMirror）
                var editor = document.querySelector(
                    'div.tiptap.ProseMirror, div.ProseMirror[contenteditable="true"]'
                );
                if (!editor) return {{ ok: false, reason: 'editor_not_found' }};

                function sleep(ms) {{
                    return new Promise(r => setTimeout(r, ms));
                }}

                // 将光标移到编辑器内容的末尾
                function moveCaretToEnd(el) {{
                    el.focus();
                    var s = window.getSelection();
                    if (!s) return;
                    var r = document.createRange();
                    r.selectNodeContents(el);
                    r.collapse(false);
                    s.removeAllRanges();
                    s.addRange(r);
                }}

                // 在光标位置插入文本（模拟真实输入）
                function insertText(text) {{
                    var ok = false;
                    try {{ ok = document.execCommand('insertText', false, text); }} catch(e) {{}}
                    if (!ok) {{
                        var s = window.getSelection();
                        if (s && s.rangeCount > 0) {{
                            var r = s.getRangeAt(0);
                            var n = document.createTextNode(text);
                            r.insertNode(n);
                            r.setStartAfter(n);
                            r.collapse(true);
                            s.removeAllRanges();
                            s.addRange(r);
                        }}
                    }}
                    // 触发 input 事件，让编辑器感知到内容变化
                    editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}

                // 模拟按下回车键（用于确认标签建议）
                function pressEnter(el) {{
                    var e = {{ key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true, cancelable:true }};
                    el.dispatchEvent(new KeyboardEvent('keydown', e));
                    el.dispatchEvent(new KeyboardEvent('keypress', e));
                    el.dispatchEvent(new KeyboardEvent('keyup', e));
                }}

                moveCaretToEnd(editor);
                // 第一个标签前先换行，和正文隔开
                if ({index} === 0) insertText({newline_lit});
                // 输入 "#" 触发标签模式
                insertText({hash_lit});
                await sleep({hash_pause});

                // 逐字输入标签文字
                var tag = {escaped_tag};
                for (var i = 0; i < tag.length; i++) {{
                    insertText(tag[i]);
                    await sleep(Math.floor(Math.random() * ({char_max} - {char_min} + 1)) + {char_min});
                }}

                // 等待建议弹窗出现
                await sleep({suggest_wait});
                // 回车确认选中的标签
                pressEnter(editor);
                await sleep({after_enter});
                // 插入空格，为下一个标签做准备
                insertText({space_lit});
                return {{ ok: true }};
            }})()
        """)

        if isinstance(result, dict) and result.get("ok"):
            log.info("标签输入成功：%s", tag)
        else:
            log.warning("标签输入失败：%s", tag)

        # 标签之间的间隔
        if index < len(tags) - 1:
            time.sleep(random.uniform(0.3, 0.7))


# ---------------------------------------------------------------------------
# 主发布函数
# ---------------------------------------------------------------------------

def publish_to_xhs(
    title: str,
    content: str,
    image_paths: list[str] | None = None,
    video_path: str | None = None,
    tags: list[str] | None = None,
    config: PublishConfig | None = None,
) -> PublishResult:
    """发布内容到小红书 — 核心入口函数

    完整流程：
        启动 Chrome → 检查登录 → 填写表单 → 输入话题标签 → 点击发布

    小红书帖子有两种模式（互斥）：
    - 图文模式：传 image_paths，不传 video_path
    - 视频模式：传 video_path，不传 image_paths
    不能同时传图片和视频（小红书不支持）。

    Args:
        title:       笔记标题
        content:     笔记正文
        image_paths: 图片文件路径列表（图文模式）
        video_path:  视频文件路径（视频模式，与 image_paths 互斥）
        tags:        话题标签列表，如 ["#美食", "#推荐"]。
                     如果为空，会尝试从 content 末尾自动提取。
        config:      发布配置，None 则使用默认值（headless + 默认账号 + 立即发布）

    Returns:
        PublishResult，包含发布状态、笔记链接或错误信息
    """
    if config is None:
        config = PublishConfig()

    # 解析账号名称（单账号场景下自动用 "default"）
    account_name = _resolve_account(config.account)
    is_video = bool(video_path)

    # 如果没有显式传入标签，尝试从正文末尾提取
    if not tags:
        content, tags = _extract_tags_from_content(content)

    log.info(
        "正在发布到小红书：title=%s, images=%d, video=%s, tags=%d, account=%s",
        title[:30], len(image_paths or []), bool(video_path), len(tags or []), account_name,
    )

    try:
        # ── 加锁：同一时间只允许一个发布任务运行 ──
        with single_instance("digitalmediaworker_xhs_publish"):

            # ── 第 1 步：确保 Chrome 已启动 ──
            # 只在本机模式下启动 Chrome（远程 CDP 模式跳过）
            if config.host in ("127.0.0.1", "localhost", "::1"):
                if not ensure_chrome(port=config.port, headless=config.headless, account=config.account):
                    return PublishResult(success=False, status="ERROR", error="Chrome 启动失败")

            # ── 第 2 步：连接 Chrome 并检查登录状态 ──
            publisher = XiaohongshuPublisher(
                host=config.host,
                port=config.port,
                timing_jitter=config.timing_jitter,
                account_name=account_name,
            )
            publisher.connect(reuse_existing_tab=config.reuse_existing_tab)

            logged_in = publisher.check_login()
            if not logged_in:
                # 未登录处理：headless 模式下自动切到 headed 模式弹出浏览器，让用户扫码
                publisher.disconnect()
                if config.headless and config.host in ("127.0.0.1", "localhost", "::1"):
                    log.warning("未登录（headless 模式），正在切换到有界面模式以便扫码登录...")
                    restart_chrome(port=config.port, headless=False, account=config.account)
                    publisher.connect(reuse_existing_tab=config.reuse_existing_tab)
                    publisher.open_login_page()
                return PublishResult(success=False, status="NOT_LOGGED_IN", error="请扫码登录小红书")

            # ── 第 3 步：填写发布表单 ──
            if is_video:
                # 视频模式：上传视频 → 填标题 → 填正文
                publisher.publish_video(title=title, content=content, video_path=video_path)
            else:
                # 图文模式：上传图片 → 填标题 → 填正文
                publisher.publish(
                    title=title, content=content,
                    image_paths=image_paths or [],
                    post_time=config.post_time,
                )

            # ── 第 4 步：在编辑器中输入话题标签 ──
            _select_topics(publisher, tags or [], timing_jitter=config.timing_jitter)

            # ── 第 5 步：发布或预览 ──
            if config.preview:
                # 预览模式：表单已填好，不点发布，留给用户手动检查
                publisher.disconnect()
                log.info("预览模式 — 表单已填写，未点击发布")
                return PublishResult(success=True, status="READY_TO_PUBLISH")

            # post_time 不为 None 时点击"定时发布"按钮，否则点击"发布"按钮
            note_link = publisher._click_publish(config.post_time is not None)
            publisher.disconnect()

            log.info("发布成功！链接=%s", note_link)
            return PublishResult(success=True, status="PUBLISHED", note_link=note_link)

    except SingleInstanceError:
        return PublishResult(success=False, status="ERROR", error="另一个发布任务正在运行，请稍后重试")
    except CDPError as e:
        return PublishResult(success=False, status="ERROR", error=str(e))
    except Exception as e:
        log.exception("小红书发布失败")
        return PublishResult(success=False, status="ERROR", error=str(e))


# ---------------------------------------------------------------------------
# 独立测试 CLI
# ---------------------------------------------------------------------------
# 直接运行此文件可以测试发布功能，不需要启动 Slack Bot
# 用法：python -m services.xhs_publisher --title "标题" --content "正文" --images img.jpg
#
# 常用参数：
#   --title     笔记标题（必填）
#   --content   笔记正文（必填）
#   --images    图片路径，可传多个（图文模式）
#   --video     视频路径（视频模式，与 --images 互斥）
#   --tags      话题标签，如 "#美食" "#推荐"
#   --preview   只填表单不发布（用于检查内容是否正确）
#   --account   指定账号名称（默认用 default）
#   --port      CDP 端口（默认 9222）

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="小红书发布测试")
    parser.add_argument("--title", required=True, help="笔记标题")
    parser.add_argument("--content", required=True, help="笔记正文")
    parser.add_argument("--images", nargs="+", help="图片路径（可多个）")
    parser.add_argument("--video", help="视频路径")
    parser.add_argument("--tags", nargs="+", help="话题标签，如 #美食 #推荐")
    parser.add_argument("--preview", action="store_true", help="只填表不发布")
    parser.add_argument("--account", help="账号名称（默认用 default）")
    parser.add_argument("--port", type=int, default=9222, help="CDP 端口（默认 9222）")
    args = parser.parse_args()

    cfg = PublishConfig(
        port=args.port,
        headless=False,  # CLI 测试时用有界面模式，方便观察
        account=args.account,
        preview=args.preview,
    )

    result = publish_to_xhs(
        title=args.title,
        content=args.content,
        image_paths=args.images,
        video_path=args.video,
        tags=args.tags,
        config=cfg,
    )

    print(f"\n发布结果：{result}")
