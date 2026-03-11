# 小红书 CDP 自动发布模块 — 改编自 XiaohongshuSkills
# https://github.com/white0dew/XiaohongshuSkills
#
# 本模块包含：
# - cdp_publish.py      — CDP 核心发布器（WebSocket 操控 Chrome）
# - chrome_launcher.py  — Chrome 进程管理（启动/关闭/重启）
# - account_manager.py  — 多账号管理（独立 profile 目录）
# - run_lock.py         — 单实例锁（防止并发发布冲突）
# - image_downloader.py — 媒体下载器（从 URL 下载图片/视频）
# - feed_explorer.py    — 搜索与笔记详情提取
# - publish_pipeline.py — CLI 统一发布入口
