"""Seedance 1.5 Pro 视频生成客户端 — 火山引擎 API

从 digital-ads-worker 项目复制并适配，直接集成到本项目中。
支持：文生视频、图生视频（用参考图作为首帧）。
"""

import os
import time
import base64
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class SeedanceClient:
    """火山引擎 Seedance 1.5 Pro 视频生成 API 客户端"""

    BASE_URL = "https://ark.cn-beijing.volces.com"
    CREATE_ENDPOINT = "/api/v3/contents/generations/tasks"

    def __init__(self, api_key: str = None, model: str = None,
                 default_resolution: str = None, default_ratio: str = None):
        self.api_key = api_key or os.getenv("VOLCENGINE_API_KEY")
        self.model = model or os.getenv("SEEDANCE_MODEL")
        if not self.model:
            raise ValueError("未配置视频模型，请在 .env 中设置 SEEDANCE_MODEL")
        self.default_resolution = default_resolution or os.getenv("DEFAULT_RESOLUTION", "720p")
        self.default_ratio = default_ratio or os.getenv("DEFAULT_RATIO", "16:9")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _encode_image_base64(self, image_path: Path) -> str:
        """将图片编码为 base64"""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def create_video_task(self, content: list, duration: int = 5,
                          resolution: str = None, ratio: str = None,
                          watermark: bool = False, camera_fixed: bool = None) -> str:
        """创建视频生成任务（异步），返回 task_id"""
        payload = {
            "model": self.model,
            "content": content,
            "duration": duration,
            "resolution": resolution or self.default_resolution,
            "ratio": ratio or self.default_ratio,
            "watermark": watermark,
            "seed": -1,
        }
        if camera_fixed is not None:
            payload["camera_fixed"] = camera_fixed

        response = self.session.post(
            f"{self.BASE_URL}{self.CREATE_ENDPOINT}",
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        task_id = result.get("id")
        if not task_id:
            raise ValueError(f"API 未返回 task_id: {result}")
        return task_id

    def wait_for_completion(self, task_id: str, timeout: int = 300,
                            poll_interval: int = 5) -> str:
        """轮询等待任务完成，返回视频 URL"""
        start = time.time()
        while True:
            elapsed = time.time() - start
            if elapsed > timeout:
                raise TimeoutError(f"视频生成超时 ({timeout}秒)")

            response = self.session.get(
                f"{self.BASE_URL}{self.CREATE_ENDPOINT}/{task_id}",
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            status = result.get("status", "unknown")

            if status == "succeeded":
                video_url = result.get("content", {}).get("video_url")
                if not video_url:
                    raise ValueError(f"任务成功但无 video_url: {result}")
                return video_url

            if status in ("failed", "expired", "cancelled"):
                err_msg = result.get("error", {}).get("message", "未知错误")
                raise Exception(f"视频生成失败 ({status}): {err_msg}")

            time.sleep(poll_interval)

    def download_video(self, video_url: str, output_path: Path) -> Path:
        """下载视频到本地"""
        response = requests.get(video_url, stream=True, timeout=60)
        response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return output_path

    # ── 便捷方法 ──────────────────────────────────────────────

    def generate_from_text(self, prompt: str, output_path: Path,
                           duration: int = 5, ratio: str = None,
                           timeout: int = 300) -> Path:
        """文生视频：纯文本 → 视频"""
        content = [{"type": "text", "text": prompt}]
        task_id = self.create_video_task(
            content=content, duration=duration, ratio=ratio,
        )
        video_url = self.wait_for_completion(task_id, timeout=timeout)
        self.download_video(video_url, output_path)
        return output_path

    def generate_from_image(self, image_path: Path, prompt: str,
                            output_path: Path, duration: int = 5,
                            ratio: str = None, timeout: int = 300) -> Path:
        """图生视频：参考图（首帧） + 文本 → 视频"""
        image_b64 = self._encode_image_base64(image_path)
        content = [
            {"type": "text", "text": prompt},
            {"type": "image", "image": image_b64},
        ]
        task_id = self.create_video_task(
            content=content, duration=duration, ratio=ratio,
        )
        video_url = self.wait_for_completion(task_id, timeout=timeout)
        self.download_video(video_url, output_path)
        return output_path
