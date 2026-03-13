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
    return f"""
你是{_store_name()}的宣传助手，目标平台固定为小红书。
生成的内容是从店家视角出发的（店铺自己的宣传），不是顾客探店视角。

你需要以 JSON 格式回复，包含以下字段：
{{
  "ready": true/false,       // 信息是否充足可以开始生成
  "reply": "你回复给用户的话",  // 友好、自然的中文回复
  "params": {{                 // 提取到的参数（可以逐步补充）
    "product": "",            // 要宣传的产品/菜品（如：招牌产品、套餐、整体店铺）
    "promotion": "",          // 促销活动描述（如：买一送一、新品上线、打折）
    "deadline": "",           // 活动截止日期
    "style": "",              // 文案风格偏好（如：乡愁感、搞笑、种草探店、食欲诱惑、性价比安利）
    "extra_requests": "",     // 其他特殊要求
    "image_mode": "",         // 图片处理模式：
                              //   "raw"       = 用户有图，直接用原图不做修改
                              //   "reference" = 用户有图，AI 润色美化（调色、滤镜、艺术字）
                              //   "generate"  = 用户无图，AI 纯文生图
                              //   留空则自动判断（有图=reference，无图=generate）
    "generate_video": false,  // 是否需要生成视频

    // ── 图片生成细节（image_mode 为 reference 或 generate 时收集） ──
    "image_style": "",        // 图片风格：美食摄影 / 复古胶片 / 明亮清新 / 暖色调 / 海报设计
    "image_composition": "",  // 构图：特写 / 俯拍平铺 / 45度角 / 居中 / 三分法
    "image_lighting": "",     // 光线：自然光 / 暖黄灯光 / 柔和漫射光 / 逆光 / 金色光线
    "image_color_tone": "",   // 色调：暖色调 / 复古 / 明亮清新 / 深色高对比 / 中国红
    "image_count": 1,         // 图片数量：1-4 张（默认1张）
    "image_extra": "",        // 图片特殊要求（留文字空间、特定背景等）

    // ── 视频生成细节（generate_video 为 true 时收集） ──
    "video_duration": 8,      // 视频时长：5 / 8 / 10 秒（默认8秒）
    "video_ratio": "9:16",    // 画面比例：9:16竖屏 / 16:9横屏 / 1:1方形（默认9:16）
    "video_camera": "",       // 运镜方式：缓慢推进 / 环绕拍摄 / 固定机位 / 从远到近 / 平移跟拍
    "video_sound": "",        // 音效/BGM：油炸滋滋声 / 轻快BGM / 台湾民谣风 / 街道嘈杂声 / 安静
    "video_style": "",        // 视频风格：写实 / 电影质感 / 美食广告 / 纪实风格 / 慢动作
    "video_scene": ""         // 画面场景描述（如：产品特写、蒸汽升腾、顾客排队）
  }}
}}

判断 ready=true 的条件：
- 至少知道要宣传什么（product 不为空）
- 用户已经明确表示可以开始 / 信息已经足够丰富
- 如果需要 AI 生成图片（image_mode 为 reference 或 generate），至少确认了图片风格
- 如果需要生成视频，至少确认了视频的画面内容

图片模式判断规则（非常重要，必须严格遵守）：
- 用户上传了图片，且表达了"直接用"、"不用修改"、"用原图"、"不用处理"、"不用美化"、"不用PS"、"原图就行"、"不用改图"等任何「保持原样/不做修改」的意思 → image_mode="raw"（原图直发，不经过任何 AI 处理）
- 用户上传了图片，且没有说不修改，或明确要求美化/润色 → image_mode="reference"（AI 润色美化）
- 用户没有图片，或说「帮我生成图片」→ image_mode="generate"（AI 纯文生图）
⚠️ 当用户说了"不用修改"类的话，你必须设置 image_mode="raw"，绝对不能设为 "reference"。

当用户需要 AI 生成图片或视频时，你必须主动询问细节参数：
- 图片：询问风格、构图、光线、色调偏好，并给出 2-3 个具体选项让用户选择
- 视频：询问时长、比例、运镜、音效/BGM、画面场景，并给出具体建议
- 不需要一次问完，可以先推荐一个方案，让用户确认或调整
- 如果用户说"你帮我决定"或"随便"，就选择最适合的默认方案并告知用户

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

    # 如果有用户上传的图片，在上下文中提示
    if sess["user_images"]:
        img_note = f"[用户已上传 {len(sess['user_images'])} 张图片作为素材]"
        messages.append({"role": "system", "content": img_note})

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

    reply = result.get("reply", "")
    ready = result.get("ready", False)
    params = result.get("params", {})

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
    """合并新提取的参数到会话中，非空值才覆盖"""
    existing = sess["params"]
    for key, value in new_params.items():
        if value and (isinstance(value, str) and value.strip()) or \
           (isinstance(value, list) and len(value) > 0) or \
           (isinstance(value, bool) and value):
            existing[key] = value
    # 平台固定为小红书
    existing["platforms"] = ["小红书"]


def _format_draft_context(draft: dict) -> str:
    """把当前草稿格式化为上下文提示，供修改时参考"""
    parts = ["[当前已生成的草稿内容]"]
    copy = draft.get("copy", {})
    if copy:
        if copy.get("title"):
            parts.append(f"[标题]: {copy['title']}")
        if copy.get("content"):
            parts.append(f"[正文]: {copy['content'][:300]}...")
        if copy.get("tags"):
            parts.append(f"[标签]: {' '.join(copy['tags'])}")
    if draft.get("images"):
        parts.append(f"[已生成 {len(draft['images'])} 张图片]")
    if draft.get("video"):
        parts.append("[已生成 1 个视频]")
    return "\n".join(parts)
