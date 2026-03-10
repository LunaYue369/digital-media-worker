"""Slack Block Kit 消息构建器 — 生成结果展示 + 操作按钮

用于在 Slack 中展示：
- 各平台文案（可复制）
- 用量统计
- 操作按钮（满意/重新生成）
"""


def build_result_message(copy_dict: dict, image_paths: list,
                         video_path: str | None, usage: dict) -> list:
    """构建完整的结果展示消息（Block Kit 格式）

    Args:
        copy_dict: {平台名: 文案文本}
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
        "text": {"type": "plain_text", "text": "宣传方案已生成", "emoji": True}
    })

    blocks.append({"type": "divider"})

    # 各平台文案
    for platform, text in copy_dict.items():
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{platform} 文案：*",
            }
        })
        # 用 code block 方便复制
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```\n{text}\n```",
            }
        })

    blocks.append({"type": "divider"})

    # 素材统计
    media_summary = []
    if image_paths:
        media_summary.append(f"图片 {len(image_paths)} 张（已上传到本频道）")
    if video_path:
        media_summary.append("视频 1 个（已上传到本频道）")
    if not media_summary:
        media_summary.append("无图片/视频素材")

    # 用量统计
    total_tokens = usage["prompt_tokens"] + usage["completion_tokens"]
    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"素材：{'，'.join(media_summary)}\n"
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
            "text": "运营同学可以直接复制文案，长按保存图片/视频，发到对应平台。",
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
