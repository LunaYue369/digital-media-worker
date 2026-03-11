"""Chrome 浏览器启动器 — 管理 CDP 远程调试模式的 Chrome 实例

为小红书自动发布提供专用的 Chrome 实例管理：
- 检测指定端口上是否已有 Chrome 在运行
- 启动 Chrome 并指定独立的 user-data-dir（用于登录态持久化）
- 等待调试端口就绪后返回
- 支持 headless（无界面）模式，适合自动化发布
- 支持 headless ↔ headed 模式切换（如未登录时弹出浏览器扫码）
- 支持多账号，每个账号使用独立的 Chrome profile 目录
"""

import os
import sys
import time
import socket
import subprocess
from typing import Optional

# ── 默认配置 ──
CDP_PORT = 9222                     # Chrome 远程调试端口
PROFILE_DIR_NAME = "XiaohongshuProfile"  # 单账号 fallback 时的 profile 目录名
STARTUP_TIMEOUT = 15                # 等待 Chrome 启动的超时时间（秒）

# ── 模块级状态 ──
# 记录我们启动的 Chrome 进程，方便后续 kill
_chrome_process: subprocess.Popen | None = None
# 记录当前正在使用的账号名称
_current_account: Optional[str] = None


def get_chrome_path() -> str:
    """查找 Chrome 可执行文件路径

    按平台依次检查常见安装路径：
    - Windows: Program Files / LocalAppData 下的 chrome.exe
    - macOS: /Applications 和 ~/Applications 下的 Google Chrome
    - Linux: /usr/bin 下的 google-chrome / chromium

    如果常见路径都找不到，使用 shutil.which 搜索 PATH。

    Raises:
        FileNotFoundError: 找不到 Chrome
    """
    candidates = []

    if sys.platform == "win32":
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var, "")
            if base:
                candidates.append(
                    os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
        )
    else:
        candidates.extend(
            [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
            ]
        )

    for path in candidates:
        if os.path.isfile(path):
            return path

    # 常见路径都没找到，用 shutil.which 搜索 PATH
    import shutil
    found = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium-browser")
        or shutil.which("chromium")
        or shutil.which("chrome")
        or shutil.which("chrome.exe")
    )
    if found:
        return found

    raise FileNotFoundError(
        "未找到 Chrome 浏览器。请安装 Google Chrome 或手动设置路径。"
    )


def get_user_data_dir(account: Optional[str] = None) -> str:
    """获取指定账号的 Chrome profile 目录路径

    优先通过 account_manager 模块获取（支持多账号隔离）。
    如果 account_manager 不可用，回退到单目录模式。

    Args:
        account: 账号名称。None 则使用默认账号。

    Returns:
        Chrome --user-data-dir 路径
    """
    try:
        from account_manager import get_profile_dir
        return get_profile_dir(account)
    except ImportError:
        # account_manager 不可用时的 fallback
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if not local_app_data:
            local_app_data = os.path.expanduser("~")
        return os.path.join(local_app_data, "Google", "Chrome", PROFILE_DIR_NAME)


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """检查指定 TCP 端口是否正在监听（是否有服务在运行）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            return False


def launch_chrome(
    port: int = CDP_PORT,
    headless: bool = False,
    account: Optional[str] = None,
) -> subprocess.Popen | None:
    """启动 Chrome 并开启远程调试端口

    如果目标端口上已有 Chrome 在运行，则跳过启动直接返回 None。

    启动参数说明：
    - --remote-debugging-port: 开启 CDP 远程调试
    - --user-data-dir: 指定独立的 profile 目录（cookie 隔离）
    - --no-first-run: 跳过首次运行的欢迎页
    - --no-default-browser-check: 跳过默认浏览器检查弹窗
    - --headless=new: 无界面模式（Chrome 110+ 新语法）

    Args:
        port:     CDP 远程调试端口（默认 9222）
        headless: 是否以无界面模式启动
        account:  账号名称，None 则使用默认账号

    Returns:
        新启动的 Popen 进程对象，如果 Chrome 已在运行则返回 None
    """
    global _chrome_process, _current_account

    if is_port_open(port):
        print(f"[chrome_launcher] Chrome 已在端口 {port} 上运行。")
        return None

    chrome_path = get_chrome_path()
    user_data_dir = get_user_data_dir(account)
    _current_account = account

    cmd = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    if headless:
        cmd.append("--headless=new")

    mode_label = "无界面" if headless else "有界面"
    account_label = account or "default"
    print(f"[chrome_launcher] 正在启动 Chrome（{mode_label}，账号：{account_label}）...")
    print(f"  可执行文件：{chrome_path}")
    print(f"  Profile 目录：{user_data_dir}")
    print(f"  调试端口：{port}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _chrome_process = proc

    # 等待调试端口就绪
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if is_port_open(port):
            print(f"[chrome_launcher] Chrome 已就绪（端口 {port}）。")
            return proc
        time.sleep(0.5)

    print(
        f"[chrome_launcher] 警告：Chrome 已启动但端口 {port} 在 {STARTUP_TIMEOUT} 秒后仍未响应，"
        f"可能还在初始化中。",
        file=sys.stderr,
    )
    return proc


def kill_chrome(port: int = CDP_PORT):
    """关闭指定调试端口上的 Chrome 实例

    依次尝试三种策略：
    1. 通过 CDP WebSocket 发送 Browser.close 命令（最优雅）
    2. terminate 我们启动的子进程
    3. Windows 下用 taskkill 按端口强杀（兜底）
    """
    global _chrome_process

    # ── 策略 1：CDP Browser.close 命令 ──
    try:
        import requests
        resp = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
        if resp.ok:
            ws_url = resp.json().get("webSocketDebuggerUrl")
            if ws_url:
                import websockets.sync.client as ws_client
                ws = ws_client.connect(ws_url)
                ws.send('{"id":1,"method":"Browser.close"}')
                try:
                    ws.recv(timeout=2)
                except Exception:
                    pass
                ws.close()
                print("[chrome_launcher] 已通过 CDP 发送 Browser.close 命令。")
    except Exception:
        pass

    # 等待 Chrome 关闭
    time.sleep(1)

    # ── 策略 2：终止我们追踪的子进程 ──
    if _chrome_process and _chrome_process.poll() is None:
        try:
            _chrome_process.terminate()
            _chrome_process.wait(timeout=5)
            print("[chrome_launcher] 已终止 Chrome 子进程。")
        except Exception:
            try:
                _chrome_process.kill()
            except Exception:
                pass
    _chrome_process = None

    # ── 策略 3：Windows 下按端口强杀（兜底） ──
    if sys.platform == "win32" and is_port_open(port):
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=5
                    )
                    print(f"[chrome_launcher] 已通过 taskkill 杀死进程 {pid}。")
                    break
        except Exception:
            pass

    # 等待端口释放
    deadline = time.time() + 5
    while time.time() < deadline:
        if not is_port_open(port):
            return
        time.sleep(0.5)

    if is_port_open(port):
        print(f"[chrome_launcher] 警告：端口 {port} 在关闭尝试后仍被占用。",
              file=sys.stderr)


def restart_chrome(
    port: int = CDP_PORT,
    headless: bool = False,
    account: Optional[str] = None,
) -> subprocess.Popen | None:
    """重启 Chrome — 先关闭再重新启动

    常见用途：
    - headless 模式下发现未登录，切换到 headed 模式让用户扫码
    - 切换账号时需要重启以加载不同的 profile 目录

    Args:
        port:     CDP 远程调试端口
        headless: 重启后是否以无界面模式运行
        account:  账号名称

    Returns:
        新启动的 Popen 进程对象
    """
    account_label = account or "default"
    mode_label = "无界面" if headless else "有界面"
    print(f"[chrome_launcher] 正在重启 Chrome（{mode_label}，账号：{account_label}）...")
    kill_chrome(port)
    time.sleep(1)
    return launch_chrome(port, headless=headless, account=account)


def ensure_chrome(
    port: int = CDP_PORT,
    headless: bool = False,
    account: Optional[str] = None,
) -> bool:
    """确保 Chrome 已在指定端口上运行

    如果已在运行，直接返回 True（忽略 headless 参数，不会重启）。
    如果未运行，启动一个新实例。

    Args:
        port:     CDP 远程调试端口
        headless: 启动新实例时是否用无界面模式（已运行时忽略此参数）
        account:  账号名称

    Returns:
        True 表示 Chrome 可用，False 表示启动失败
    """
    if is_port_open(port):
        return True
    try:
        launch_chrome(port, headless=headless, account=account)
        return is_port_open(port)
    except FileNotFoundError as e:
        print(f"[chrome_launcher] 错误：{e}", file=sys.stderr)
        return False


def get_current_account() -> Optional[str]:
    """获取当前正在使用的账号名称"""
    return _current_account


# ---------------------------------------------------------------------------
# CLI 命令行工具
# ---------------------------------------------------------------------------
# 直接运行此文件可以手动管理 Chrome 实例
# 用法：
#   python chrome_launcher.py                    # 启动 Chrome（有界面模式）
#   python chrome_launcher.py --headless         # 启动 Chrome（无界面模式）
#   python chrome_launcher.py --kill             # 关闭 Chrome
#   python chrome_launcher.py --restart          # 重启 Chrome
#   python chrome_launcher.py --account myaccount  # 指定账号

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Chrome 启动器（CDP 远程调试）")
    parser.add_argument("--port", type=int, default=CDP_PORT,
                        help=f"CDP 远程调试端口（默认 {CDP_PORT}）")
    parser.add_argument("--headless", action="store_true", help="以无界面模式启动")
    parser.add_argument("--kill", action="store_true", help="关闭正在运行的 Chrome 实例")
    parser.add_argument("--restart", action="store_true", help="重启 Chrome")
    parser.add_argument("--account", help="账号名称（默认用 default）")
    args = parser.parse_args()

    if args.kill:
        kill_chrome(port=args.port)
        print("[chrome_launcher] Chrome 已关闭。")
    elif args.restart:
        restart_chrome(port=args.port, headless=args.headless, account=args.account)
        print("[chrome_launcher] Chrome 已重启。")
    elif ensure_chrome(port=args.port, headless=args.headless, account=args.account):
        print("[chrome_launcher] Chrome 已就绪，可以接受 CDP 连接。")
    else:
        print("[chrome_launcher] Chrome 启动失败。", file=sys.stderr)
        sys.exit(1)
