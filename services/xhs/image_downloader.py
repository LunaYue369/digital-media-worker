"""媒体下载器 — 从 URL 下载图片和视频到本地临时目录

用于 publish_pipeline.py（CLI 模式）从远程 URL 下载图片/视频后上传到小红书。
Bot 模式下图片已经在本地（用户上传或 AI 生成），通常不走这个下载器。

功能：
- 下载图片 URL 到临时目录，自动推测文件扩展名
- 下载视频 URL 到临时目录
- 发布完成后清理临时文件
- 支持 Referer 头绕过防盗链保护
- 支持 context manager（with 语句自动清理）

用法：
    with ImageDownloader() as dl:
        paths = dl.download_all(["https://example.com/img1.jpg", ...])
        # 用 paths 去发布...
    # 退出 with 后自动清理临时文件
"""

import os
import sys
import tempfile
import shutil
import uuid
from urllib.parse import urlparse, unquote

import requests

# 单个文件的下载超时时间（秒）
DEFAULT_TIMEOUT = 30
# 临时目录前缀
TEMP_DIR_PREFIX = "xhs_images_"


class ImageDownloader:
    """媒体下载器 — 下载图片/视频 URL 并管理临时目录

    Args:
        temp_dir: 自定义临时目录路径。None 则自动创建一个。
    """

    def __init__(self, temp_dir: str | None = None):
        if temp_dir:
            # 使用指定的目录（不会在清理时删除目录本身）
            self.temp_dir = temp_dir
            os.makedirs(self.temp_dir, exist_ok=True)
            self._owns_dir = False
        else:
            # 自动创建临时目录（清理时会删除整个目录）
            self.temp_dir = tempfile.mkdtemp(prefix=TEMP_DIR_PREFIX)
            self._owns_dir = True
        # 记录所有已下载的文件路径
        self.downloaded_files: list[str] = []

    def _guess_extension(self, url: str, content_type: str | None) -> str:
        """推测图片文件扩展名

        优先从 URL 路径提取，提取不到则根据 Content-Type 响应头推断。
        都不行则默认 .jpg。
        """
        # 先尝试从 URL 路径提取
        path = urlparse(url).path
        _, ext = os.path.splitext(unquote(path))
        if ext and ext.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            return ext.lower()

        # 再根据 Content-Type 推断
        ct_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }
        if content_type:
            for mime, ext in ct_map.items():
                if mime in content_type:
                    return ext

        return ".jpg"  # 兜底默认

    def _guess_video_extension(self, url: str, content_type: str | None) -> str:
        """推测视频文件扩展名

        逻辑同 _guess_extension，但匹配视频格式。
        """
        path = urlparse(url).path
        _, ext = os.path.splitext(unquote(path))
        if ext and ext.lower() in (".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm"):
            return ext.lower()

        ct_map = {
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/x-msvideo": ".avi",
            "video/x-matroska": ".mkv",
            "video/x-flv": ".flv",
            "video/x-ms-wmv": ".wmv",
            "video/webm": ".webm",
        }
        if content_type:
            for mime, ext in ct_map.items():
                if mime in content_type:
                    return ext

        return ".mp4"  # 兜底默认

    def download(self, url: str, referer: str | None = None) -> str:
        """下载单张图片，返回本地文件路径

        Args:
            url:     图片 URL
            referer: Referer 请求头。None 则自动从 URL 域名生成。
                     某些图床有防盗链保护，需要正确的 Referer 才能下载。

        Returns:
            下载后的本地文件路径

        Raises:
            requests.RequestException: 网络错误
        """
        # 构建请求头（带 Referer 绕过防盗链）
        parsed = urlparse(url)
        if referer is None:
            referer = f"{parsed.scheme}://{parsed.netloc}/"

        headers = {
            "Referer": referer,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        resp = requests.get(url, timeout=DEFAULT_TIMEOUT, stream=True, headers=headers)
        resp.raise_for_status()

        ext = self._guess_extension(url, resp.headers.get("Content-Type"))
        filename = f"{uuid.uuid4().hex[:12]}{ext}"
        filepath = os.path.join(self.temp_dir, filename)

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        self.downloaded_files.append(filepath)
        print(f"[image_downloader] 已下载：{url}")
        print(f"  → {filepath}（{os.path.getsize(filepath)} 字节）")
        return filepath

    def download_video(self, url: str, referer: str | None = None) -> str:
        """下载单个视频，返回本地文件路径

        与 download() 类似，但超时时间更长（视频文件通常更大），
        且使用更大的 chunk_size 提高下载效率。

        Args:
            url:     视频 URL
            referer: Referer 请求头

        Returns:
            下载后的本地文件路径
        """
        parsed = urlparse(url)
        if referer is None:
            referer = f"{parsed.scheme}://{parsed.netloc}/"

        headers = {
            "Referer": referer,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        # 视频文件较大，超时时间 × 4
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT * 4, stream=True, headers=headers)
        resp.raise_for_status()

        ext = self._guess_video_extension(url, resp.headers.get("Content-Type"))
        filename = f"{uuid.uuid4().hex[:12]}{ext}"
        filepath = os.path.join(self.temp_dir, filename)

        with open(filepath, "wb") as f:
            # 视频用更大的 chunk（64KB）加速下载
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        self.downloaded_files.append(filepath)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"[image_downloader] 已下载视频：{url}")
        print(f"  → {filepath}（{size_mb:.1f} MB）")
        return filepath

    def download_all(self, urls: list[str]) -> list[str]:
        """批量下载图片，返回成功下载的本地文件路径列表

        单个 URL 下载失败时记录错误并跳过，不影响其他 URL。
        """
        paths = []
        for url in urls:
            try:
                path = self.download(url)
                paths.append(path)
            except Exception as e:
                print(f"[image_downloader] 下载失败 {url}：{e}", file=sys.stderr)
        return paths

    def cleanup(self):
        """清理所有已下载的临时文件

        如果是自动创建的临时目录，删除整个目录。
        如果是用户指定的目录，只删除下载的文件。
        """
        if self._owns_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            print(f"[image_downloader] 已清理临时目录：{self.temp_dir}")
        else:
            for f in self.downloaded_files:
                try:
                    os.remove(f)
                except OSError:
                    pass
            print(f"[image_downloader] 已清理 {len(self.downloaded_files)} 个文件。")
        self.downloaded_files.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.cleanup()


# ---------------------------------------------------------------------------
# CLI 测试工具
# ---------------------------------------------------------------------------
# 用法：python image_downloader.py <url1> [url2] ...
# 下载指定 URL 的图片到临时目录（不会自动清理，方便检查）

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python image_downloader.py <url1> [url2] ...")
        sys.exit(1)

    dl = ImageDownloader()
    paths = dl.download_all(sys.argv[1:])
    print(f"\n已下载 {len(paths)} 张图片：")
    for p in paths:
        print(f"  {p}")
    print(f"临时目录：{dl.temp_dir}")
    print("文件将保留，需手动清理。")
