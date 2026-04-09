# Digital Media Worker

Conversational AI bot for generating social media promotional content. Users describe what they want through natural Slack dialogue, and the bot produces copywriting, AI-generated images, and AI-generated videos -- then publishes directly to Xiaohongshu (Little Red Book) via browser automation.

## Features

- **Conversational Content Creation** -- Multi-turn Slack dialogue to define promotion details; AI extracts parameters from natural conversation
- **Multi-Mode Image Processing**
  - `raw` -- Use uploaded photo as-is
  - `enhance` -- AI-beautify uploaded photo (preserve subject, add effects)
  - `reference` -- Generate new image inspired by uploaded photo
  - `generate` -- Pure text-to-image generation
  - Per-image mode selection within a single post
- **AI Video Generation** -- Text-to-video and image-to-video via Seedance 1.5 Pro
- **Xiaohongshu Auto-Publishing** -- CDP browser automation that fills title, content, uploads media, selects topic tags, and handles login (with QR code fallback)
- **Partial Modification** -- Users can request changes to specific parts (title only, a single image, video redo) without regenerating everything
- **Multi-Merchant** -- Single instance serves multiple businesses with isolated brand config

## Architecture

```
main.py                        # Slack bot entry point (Bolt, Socket Mode)
pipeline/
  promo_pipeline.py            # Content generation orchestrator
agents/
  conversation.py              # Intent parsing + parameter extraction
  media_engineer.py            # Translates descriptions into AI prompts
  copywriter.py                # Promotional copy with title, content, hashtags
  reviewer.py                  # Quality gate: up to 3 revision rounds
services/
  seedream_client.py           # Volcengine Seedream 4.5 (image generation)
  seedance_client.py           # Volcengine Seedance 1.5 Pro (video generation)
  image_processor.py           # Multi-mode image processing orchestrator
  video_generator.py           # Video generation wrapper
  xhs/
    publish_pipeline.py        # Xiaohongshu end-to-end publish flow
    cdp_publish.py             # CDP-based XHS page interaction
    chrome_launcher.py         # Chrome process management
    feed_explorer.py           # XHS feed browsing for topic discovery
    account_manager.py         # Login state + QR code handling
core/
  router.py                    # Session stage dispatcher
  session.py                   # Conversation state machine
  merchant_config.py           # Per-merchant configuration
slack_ui/
  blocks.py                    # Preview cards with Approve/Regenerate/Publish buttons
```

## Pipeline Flow

```
Slack Conversation ──> Intent Parsing ──> Parameter Extraction
        |
        v
   Media Engineer (optimize prompts) ──> Copywriter (title + content + hashtags)
        |
        v
   Image Processing (per-image mode) ──> Video Generation (optional)
        |
        v
   Review Loop (up to 3 rounds)
        |
        v
   Slack Preview (images + video + copy + action buttons)
        |
        v
   [Approve] ──> Xiaohongshu CDP Publishing
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Bot Framework | Slack Bolt (Socket Mode) |
| LLM | OpenAI GPT-4.1-mini |
| Image Generation | Volcengine Seedream 4.5 |
| Video Generation | Volcengine Seedance 1.5 Pro |
| Social Publishing | Chrome DevTools Protocol (Xiaohongshu) |
| State Management | In-memory session store with thread safety |
