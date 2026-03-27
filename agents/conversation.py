"""对话层 — GPT 理解用户意图，提取参数，判断是否可以开始生成

核心逻辑：
1. 把会话历史 + 用户最新消息发给 GPT
2. GPT 返回结构化 JSON：
   - ready: bool — 信息是否充足，可以开始生成
   - reply: str — 回复给用户的话
   - params: dict — 提取出的生成参数（产品、活动、风格等）
3. 如果 ready=True，自动进入 pipeline 执行生成

目标平台固定为小红书。
"""

import json
import logging
import os
import threading
from pathlib import Path

from openai import OpenAI

from agents.soul_loader import build_system_prompt
from core import session
from core.merchant_config import store_name as _store_name
from core.session import GENERATING
from services.usage_tracker import record_usage, estimate_cost

log = logging.getLogger(__name__)

MODEL = os.getenv("AGENT_MODEL", "gpt-4.1-mini")
_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(max_retries=3)
    return _client


# 对话层的 system prompt 后缀：指导 GPT 做意图理解和参数提取
def _build_extraction_instruction() -> str:
    return f"""你是{_store_name()}的宣传助手，目标平台固定为小红书。
生成的内容是从店家视角出发的（店铺自己的宣传），不是顾客探店视角。

你需要以 JSON 格式回复，包含以下字段：
{{
  "ready": true/false,       // 信息是否充足可以开始生成
  "reply": "你回复给用户的话",  // 友好、自然的中文回复
  "params": {{                 // 提取到的参数（可以逐步补充，留空则保留上轮的值）
                               //   如果用户明确要求取消某个已有参数（如"不要促销了"），填 "__clear__" 清空该字段
    "product": "",            // 要宣传的产品/菜品（如：招牌产品、套餐、整体店铺）
    "promotion": "",          // 促销活动描述（如：买一送一、新品上线、打折）
    "deadline": "",           // 活动截止日期
    "style": "",              // 文案风格偏好（如：乡愁感、搞笑、种草探店、食欲诱惑、性价比安利）
    "extra_requests": "",     // 其他特殊要求
    "image_mode": "",         // 图片处理模式（全局，所有图片统一处理时使用）：
                              //   "raw"       = 全部原图不修改
                              //   "enhance"   = 全部 AI 美化（加字、加滤镜、调色，保留原图主体）
                              //   "reference" = 全部以原图为参考，AI 生成全新图片
                              //   "generate"  = 无参考图，AI 纯文生图
                              //   "mixed"     = 不同图片不同处理（必须同时填 per_image_modes）
                              //   留空则自动判断（有图=enhance，无图=generate）
    "per_image_modes": [],    // 逐张图片处理模式（仅 image_mode="mixed" 时必填）
                              //   按图片编号顺序，每张一个值："raw" / "enhance" / "reference"
                              //   例：用户上传3张图，说"图1原图，图2美化，图3参考生成新图"
                              //   → ["raw", "enhance", "reference"]
                              //   长度必须等于用户上传的图片数量
    "extra_generate_count": 0, // 额外纯文生图数量（不依赖任何参考图，AI 凭文字描述生成全新图片）
                              //   适用于所有 image_mode，不仅限于 mixed 模式
                              //   例：用户说"再额外帮我AI生成2张" → extra_generate_count: 2
                              //   可以和 per_image_modes 同时使用（如：3张原图各自处理 + 额外再生成2张）
    "generate_video": false,  // 是否需要生成视频

    // ── 图片生成细节（image_mode 为 reference 或 generate 时收集） ──
    "image_style": "",        // 图片风格：美食摄影 / 复古胶片 / 明亮清新 / 暖色调 / 海报设计
    "image_composition": "",  // 构图：特写 / 俯拍平铺 / 45度角 / 居中 / 三分法
    "image_lighting": "",     // 光线：自然光 / 暖黄灯光 / 柔和漫射光 / 逆光 / 金色光线
    "image_color_tone": "",   // 色调：暖色调 / 复古 / 明亮清新 / 深色高对比 / 中国红
    "image_count": 1,         // 图片数量：1-4 张（默认1张）
    "image_extra": "",        // 图片特殊要求（如：预留空间以便叠加艺术字/店名/价格标签、指定背景颜色、加边框等）

    // ── 视频生成细节（generate_video 为 true 时收集） ──
    "video_duration": 8,      // 视频时长：5 / 8 / 10 秒（默认8秒）
    "video_ratio": "9:16",    // 画面比例：9:16竖屏 / 16:9横屏 / 1:1方形（默认9:16）
    "video_camera": "",       // 运镜方式：缓慢推进 / 环绕拍摄 / 固定机位 / 从远到近 / 平移跟拍
    "video_sound": "",        // 音效/BGM：油炸滋滋声 / 轻快BGM / 台湾民谣风 / 街道嘈杂声 / 安静
    "video_style": "",        // 视频风格：写实 / 电影质感 / 美食广告 / 纪实风格 / 慢动作
    "video_scene": "",         // 画面场景描述（如：产品特写、蒸汽升腾、顾客排队）

    // ── 局部修改（仅在已有草稿且用户要求修改时填写） ──
    "modify_scope": {{          // 修改范围（不填或为空则全部重新生成）
      "title": true/false,     // 是否重写标题
      "content": true/false,   // 是否重写正文和标签
      "images": "keep",        // 图片修改方式：
                               //   "keep" = 全部保留
                               //   "all"  = 全部重做（沿用原 image_mode 设置）
                               //   数组   = 只重做指定图片，每项指定编号和处理方式：
                               //            [{{"index": 2, "mode": "enhance"}}, {{"index": 3, "mode": "generate"}}]
                               //            mode 可选: "raw" / "enhance" / "reference" / "generate"
                               //            index 从1开始，对应草稿中的图片编号
      "video": "keep"          // "keep"=保留现有视频 / "redo"=重新生成视频
    }},
    "modify_feedback": ""      // 用户的具体修改意见（如"标题太噱头了，换个正经点的"、"正文加上早餐套餐价格"）
                               //   必须忠实提取用户原话中的修改要求，不要遗漏细节
                               //   仅在局部修改时填写，首次生成时留空
  }}
}}

判断 ready=true 的条件：
- 用户已经明确表示可以开始（如"开始吧"、"帮我做一个帖子"、"生成"等），即使没有指定 product 也可以 ready=true，此时使用 Soul 中的默认招牌产品
- 如果用户详细说明了 product 和 theme 当然更好，直接使用
- 如果需要 AI 生成图片（image_mode 为 reference 或 generate），至少确认了图片风格
- 如果需要生成视频，至少确认了视频的画面内容
- 小红书发帖必须有配图：如果用户没有上传任何图片，必须确保会生成图片（image_mode="generate" 且 image_count>=1），否则不能 ready=true

局部修改规则（当上方 [当前已生成的草稿内容] 存在时适用）：
- 只改标题（如用户说"标题太噱头了，换个正经点的"）
  → modify_scope: {{title: true, content: false, images: "keep", video: "keep"}}
  → modify_feedback: "标题太噱头了，换个正经点的"
- 只改正文（如"正文加上早餐套餐价格"）
  → modify_scope: {{title: false, content: true, images: "keep", video: "keep"}}
  → modify_feedback: "正文加上早餐套餐价格"
- 标题正文都改（如"文案重写，语气轻松一些"）
  → modify_scope: {{title: true, content: true, images: "keep", video: "keep"}}
  → modify_feedback: "语气轻松一些"
- 只改图片（如"图片风格不对，要更明亮的"）
  → modify_scope: {{title: false, content: false, images: "all", video: "keep"}}
  → modify_feedback: "图片风格不对，要更明亮的"
- 只改某几张图，按用户要求指定每张的处理方式：
  "第2张太暗了帮我美化一下，第3张重新生成"
  → modify_scope: {{title: false, content: false, images: [{{"index": 2, "mode": "enhance"}}, {{"index": 3, "mode": "generate"}}], video: "keep"}}
  → modify_feedback: "第2张太暗了美化，第3张重新生成"
- 组合修改（如"标题换一个，第3张图用原图参考重新生成"）
  → modify_scope: {{title: true, content: false, images: [{{"index": 3, "mode": "reference"}}], video: "keep"}}
  → modify_feedback: "标题换一个，第3张图参考原图重新生成"
- 只改视频 → modify_scope: {{title: false, content: false, images: "keep", video: "redo"}}
- 全部重来 → 不填 modify_scope 和 modify_feedback（走完整重新生成流程）
- 局部修改也直接设 ready=true，不需要再追问
- modify_feedback 必须忠实提取用户的修改意见原文，这是传给 copywriter/media_engineer 的关键信息

上下文注入说明（系统会在对话历史前自动注入以下信息）：

1. [用户已上传的图片清单]（如果有）：格式为"图1: 文件名, 图2: 文件名, ..."
   这些是用户从 Slack 上传的原始图片，用于首次生成时判断 image_mode 和 per_image_modes。

2. [当前已生成的草稿内容]（如果有）：包含上一版的标题、正文、标签，以及草稿图片编号列表（草稿图1, 草稿图2, ...）
   当用户在局部修改时提到"第X张图"，指的是草稿图编号（草稿图X），不是上传的原始图编号。
   modify_scope.images 里的 index 必须对应草稿图编号。

图片模式判断规则（非常重要，必须严格遵守）：

统一模式（所有图片相同处理）：
- 用户上传了图片，且说"直接用"、"不用修改"、"用原图"等 → image_mode="raw"
- 用户上传了图片，且要求美化/P图/加字/加滤镜 → image_mode="enhance"
- 用户上传了图片，且要求"参考生成新图" → image_mode="reference"
- 用户没有图片，或说「帮我生成图片」→ image_mode="generate"

混合模式（不同图片不同处理）：
- 用户对不同图片有不同要求时 → image_mode="mixed"，同时填 per_image_modes
- 例："图1原图，图2帮我美化加店名，图3参考生成新海报" → image_mode="mixed", per_image_modes=["raw", "enhance", "reference"]
- per_image_modes 长度必须等于用户上传的图片数量

额外生成：
- 用户说"再额外帮我AI生成N张" → extra_generate_count=N
- 可以和任何 image_mode 组合使用（包括 mixed）

当用户说了"不用修改"类的话，对应图片必须设为 "raw"。
当用户对不同图片有不同要求时，必须用 mixed + per_image_modes，不能用统一模式。

当用户需要 AI 生成图片或视频时，你必须主动追问细节参数，绝对不能跳过直接生成：
- 图片必问（至少第一轮要问）：image_style（风格）、image_composition（构图）、image_lighting（光线）
  → 给出 2-3 个具体选项让用户选择
  → 例："图片风格您偏好哪种？A. 美食摄影（高清特写） B. 复古胶片（暖调怀旧） C. 明亮清新（小红书风）"
- 视频必问：video_scene（画面场景描述）、video_camera（运镜方式）
  → 可以先推荐一个默认方案，让用户确认或调整
- 如果用户说"你帮我决定"或"随便"，选择最适合该产品的默认方案，并明确告知用户你的选择
- 不需要一次问完所有参数，但风格/场景这类核心参数必须在第一轮确认

如果用户的需求不清晰，友好地追问，但不要一次问太多问题。
如果用户提出修改意见（如"文案加上价格"、"换个风格"），理解意图并设置 ready=true。
平台不需要问，固定就是小红书。
"""


def chat_and_maybe_generate(sess: dict, user_text: str, say, client):
    """对话层主函数：理解用户意图，可能触发生成 pipeline"""
    thread_ts = sess["thread_ts"]

    # 构建 system prompt
    system_prompt = build_system_prompt("assistant") + "\n\n" + _build_extraction_instruction()

    # 构建消息列表（包含完整对话历史）
    messages = [{"role": "system", "content": system_prompt}]

    # 如果有用户上传的图片，在上下文中展示带编号的列表
    if sess["user_images"]:
        img_lines = [f"[用户已上传 {len(sess['user_images'])} 张图片：]"]
        for i, img_path in enumerate(sess["user_images"], 1):
            # 从路径提取原始文件名（去掉时间戳前缀 YYYYMMDD_HHMMSS_）
            fname = Path(img_path).name
            parts = fname.split("_", 2)
            original_name = parts[2] if len(parts) >= 3 else fname
            img_lines.append(f"  图{i}: {original_name}")
        messages.append({"role": "system", "content": "\n".join(img_lines)})

    # 如果有之前的草稿，在上下文中提示（用于修改场景）
    if sess["draft"]:
        draft_note = _format_draft_context(sess["draft"])
        messages.append({"role": "system", "content": draft_note})

    # 添加对话历史
    for msg in sess["messages"]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # 调用 GPT
    gpt_client = _get_client()
    resp = gpt_client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=800,
        response_format={"type": "json_object"},
    )

    # 记录 token 用量
    pt = resp.usage.prompt_tokens
    ct = resp.usage.completion_tokens
    cost = estimate_cost(pt, ct)
    session.add_usage(thread_ts, pt, ct, cost)
    record_usage(thread_ts, "conversation", pt, ct)

    # 解析 GPT 回复
    raw = resp.choices[0].message.content
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        log.error("对话层 JSON 解析失败: %s", raw[:200])
        say(text="抱歉，我没能理解您的意思，能再说一遍吗？", thread_ts=thread_ts)
        return

    reply = result.get("reply", "").strip()
    ready = result.get("ready", False)
    params = result.get("params", {})

    if not reply:
        log.warning("对话层返回空 reply，原始 JSON: %s", raw[:300])
        reply = "抱歉，我没能理解您的意思，能再说一遍吗？"

    # 更新会话参数（逐步补充，不覆盖已有值）
    _merge_params(sess, params)

    # 记录 bot 回复到对话历史
    session.add_message(thread_ts, "assistant", reply)

    if ready:
        # 信息充足，通知用户并启动 pipeline
        say(text=f"{reply}\n\n正在为您生成宣传草稿，请稍等...", thread_ts=thread_ts)
        session.update_stage(thread_ts, GENERATING)

        # 启动 pipeline（在新线程中已被 router 包装，这里直接调用）
        from pipeline.promo_pipeline import run_pipeline
        run_pipeline(sess, say, client)
    else:
        # 信息不足，回复追问
        say(text=reply, thread_ts=thread_ts)


def _merge_params(sess: dict, new_params: dict):
    """合并新提取的参数到会话中，非空值才覆盖，"__clear__" 清空对应字段"""
    existing = sess["params"]
    for key, value in new_params.items():
        if value == "__clear__":
            existing.pop(key, None)
        elif isinstance(value, bool) and value:
            existing[key] = value
        elif isinstance(value, int) and value > 0:
            existing[key] = value
        elif isinstance(value, str) and value.strip():
            existing[key] = value
        elif isinstance(value, list) and len(value) > 0:
            existing[key] = value
        elif isinstance(value, dict) and len(value) > 0:
            existing[key] = value
    # 平台固定为小红书
    existing["platforms"] = ["小红书"]


def _format_draft_context(draft: dict) -> str:
    """把当前草稿格式化为上下文提示，供修改时参考"""
    parts = ["[当前已生成的草稿内容（用户可能要求局部修改，以下是上一版结果）]"]
    copy = draft.get("copy", {})
    if copy:
        if copy.get("title"):
            parts.append(f"[标题]: {copy['title']}")
        if copy.get("content"):
            parts.append(f"[正文]: {copy['content'][:300]}...")
        if copy.get("tags"):
            parts.append(f"[标签]: {' '.join(copy['tags'])}")
    images = draft.get("images", [])
    if images:
        parts.append(f"[已生成 {len(images)} 张图片（草稿图编号）:]")
        for i, img_path in enumerate(images, 1):
            fname = Path(img_path).name
            parts.append(f"  草稿图{i}: {fname}")
    if draft.get("video"):
        parts.append("[已生成 1 个视频]")
    return "\n".join(parts)
