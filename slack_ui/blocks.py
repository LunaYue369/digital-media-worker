"""Slack Block Kit 消息构建器 — 生成结果展示 + 操作按钮

用于在 Slack 中展示：
- 各平台文案（可复制）
- 用量统计
- 操作按钮（满意/重新生成）
- 确认后的发布按钮（发布到小红书）
"""


def build_result_message(copy_dict: dict, image_paths: list,
                         video_path: str | None, usage: dict) -> list:
    """构建完整的结果展示消息（Block Kit 格式）

    Args:
        copy_dict: {"title": "标题", "content": "正文", "tags": ["#tag1", ...]}
        image_paths: 图片路径列表
        video_path: 视频路径
        usage: token 用量统计

    Returns:
        Slack Block Kit blocks 列表
    """
    blocks = []

    # 标题
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "宣传草稿已就绪", "emoji": True}
    })

    blocks.append({"type": "divider"})

    # 小红书文案 — 结构化展示
    title = copy_dict.get("title", "")
    content = copy_dict.get("content", "")
    tags = copy_dict.get("tags", [])
    tags_text = " ".join(tags) if isinstance(tags, list) else str(tags)

    # 标题
    if title:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*小红书标题：*\n```\n{title}\n```",
            }
        })

    # 正文
    if content:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*小红书正文：*\n```\n{content}\n```",
            }
        })

    # 标签
    if tags_text:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*标签：*\n{tags_text}",
            }
        })

    blocks.append({"type": "divider"})

    # 素材统计
    media_summary = []
    if image_paths:
        media_summary.append(f"已上传 {len(image_paths)} 张图片")
    if video_path:
        media_summary.append("已上传 1 个视频")
    if not media_summary:
        media_summary.append("无图片/视频素材")

    # 用量统计
    total_tokens = usage["prompt_tokens"] + usage["completion_tokens"]
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"素材：{', '.join(media_summary)}\n"
                f"用量：{usage['api_calls']} 次 API 调用 | "
                f"{total_tokens:,} tokens | "
                f"${usage['estimated_cost']:.4f}"
            ),
        }]
    })

    blocks.append({"type": "divider"})

    # 提示文字
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "满意请点击按钮，或直接打字告诉我要改什么（如「标题换一个」「第2张图重做」）。",
        }
    })

    # 操作按钮
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "满意", "emoji": True},
                "style": "primary",
                "action_id": "approve_draft",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "重新生成", "emoji": True},
                "action_id": "regenerate_draft",
            },
        ]
    })

    return blocks


def build_approved_message(usage: dict) -> list:
    """构建「用户点击满意后」的确认消息（Block Kit 格式）

    显示用量统计摘要，并提供「发布到小红书」按钮。
    用户可以选择立即发布，也可以忽略按钮自行手动发布。

    Args:
        usage: token 用量统计字典，包含 prompt_tokens / completion_tokens / api_calls / estimated_cost

    Returns:
        Slack Block Kit blocks 列表
    """
    total_tokens = usage["prompt_tokens"] + usage["completion_tokens"]

    blocks = []

    # ── 确认标题 ──
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "素材已确认", "emoji": True},
    })

    blocks.append({"type": "divider"})

    # ── 用量统计摘要 ──
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*本次用量统计：*\n"
                f"API 调用次数：{usage['api_calls']}\n"
                f"总 tokens：{total_tokens:,}\n"
                f"  - 输入：{usage['prompt_tokens']:,}\n"
                f"  - 输出：{usage['completion_tokens']:,}\n"
                f"预估费用：${usage['estimated_cost']:.4f}"
            ),
        },
    })

    blocks.append({"type": "divider"})

    # ── 提示文字：引导用户下一步操作 ──
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "素材已确认，可以直接发布到小红书，也可以自行手动发布。",
        },
    })

    # ── 发布按钮 ──
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                # 绿色主按钮，点击后触发 publish_to_xhs action
                "text": {"type": "plain_text", "text": "发布到小红书", "emoji": True},
                "style": "primary",
                "action_id": "publish_to_xhs",
            },
        ],
    })

    return blocks
