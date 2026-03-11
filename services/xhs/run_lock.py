"""单实例锁 — 防止多个发布任务同时运行

小红书发布需要操控同一个 Chrome 实例，多个发布任务并发会互相干扰。
通过文件锁机制确保同一时间只有一个发布进程在运行。

工作原理：
1. 在系统临时目录下创建一个 .lock 文件（原子操作 O_CREAT | O_EXCL）
2. 锁文件中记录当前进程的 PID、启动时间等信息
3. 如果锁文件已存在，检查持有锁的进程是否还活着：
   - 进程已死 → 清理过期锁，重新获取
   - 进程还活 → 抛出 SingleInstanceError
4. 任务完成后自动删除锁文件（通过 context manager 的 finally）

用法：
    from run_lock import single_instance, SingleInstanceError

    try:
        with single_instance("my_task"):
            # 这里执行发布逻辑...
            pass
    except SingleInstanceError as e:
        print(f"另一个任务正在运行：{e}")
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any


class SingleInstanceError(RuntimeError):
    """当已有另一个发布进程在运行时抛出"""


def _lock_path(lock_name: str) -> str:
    """根据锁名称生成锁文件的完整路径（存放在系统临时目录下）"""
    # 将锁名称中的特殊字符替换为下划线，确保文件名安全
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in lock_name)
    return os.path.join(tempfile.gettempdir(), f"{safe_name}.lock")


def _pid_running(pid: int) -> bool:
    """检查指定 PID 的进程是否还在运行

    通过 os.kill(pid, 0) 探测：
    - ProcessLookupError → 进程不存在
    - PermissionError → 进程存在但无权限（仍视为运行中）
    - 成功 → 进程存在
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但属于其他用户，视为仍在运行
        return True
    except OSError:
        return False
    return True


def _read_lock_data(path: str) -> dict[str, Any]:
    """读取锁文件中的 JSON 数据，解析失败返回空字典"""
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            data = json.load(file_handle)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_lock_data(path: str, payload: dict[str, Any]) -> None:
    """原子写入锁文件

    使用 O_CREAT | O_EXCL 标志：如果文件已存在则抛出 FileExistsError。
    这保证了在多进程竞争时只有一个进程能成功创建锁文件。
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, ensure_ascii=False)


def _cleanup_stale_lock(path: str) -> tuple[bool, dict[str, Any]]:
    """清理过期的锁文件

    检查锁文件中记录的 PID 是否还在运行：
    - 进程已死 → 删除锁文件，返回 (True, lock_data)
    - 进程还活 → 不删除，返回 (False, lock_data)

    Returns:
        (是否成功清理, 锁文件中的数据)
    """
    lock_data = _read_lock_data(path)
    pid = lock_data.get("pid")

    # 如果 PID 对应的进程还在运行，不能清理
    if isinstance(pid, int) and _pid_running(pid):
        return False, lock_data

    # 进程已死或 PID 无效，清理过期锁
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False, lock_data

    return True, lock_data


def _format_conflict_message(path: str, lock_data: dict[str, Any]) -> str:
    """生成锁冲突时的错误提示信息"""
    pid = lock_data.get("pid")
    started_at = lock_data.get("started_at")

    if isinstance(pid, int):
        msg = f"另一个发布任务正在运行（pid={pid}）"
        if isinstance(started_at, str) and started_at:
            msg += f"，启动于 {started_at}"
        return msg + "。请等待其完成或手动终止后重试。"

    return f"另一个发布任务正在运行（锁文件：{path}）。请稍后重试。"


@contextmanager
def single_instance(lock_name: str = "post_to_xhs_publish"):
    """单实例锁 context manager — 确保同一时间只有一个发布任务运行

    用法：
        with single_instance("my_publish_task"):
            # 执行发布逻辑...
            pass

    如果已有另一个任务持有锁，抛出 SingleInstanceError。

    Args:
        lock_name: 锁名称，不同的任务类型应使用不同的名称
    """
    path = _lock_path(lock_name)
    # 随机 token 用于验证锁的所有权（防止误删其他进程的锁）
    token = uuid.uuid4().hex

    # 锁文件中记录的信息，方便排查问题
    payload = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "token": token,
    }

    acquired = False
    for attempt in range(2):
        try:
            # 尝试原子创建锁文件
            _write_lock_data(path, payload)
            acquired = True
            break
        except FileExistsError:
            # 锁文件已存在 — 检查是否是过期锁
            removed, lock_data = _cleanup_stale_lock(path)
            if removed and attempt == 0:
                # 过期锁已清理，重试一次
                continue
            # 锁仍被持有，抛出冲突异常
            raise SingleInstanceError(_format_conflict_message(path, lock_data))

    if not acquired:
        raise SingleInstanceError(f"无法获取锁：{path}")

    try:
        yield
    finally:
        # 任务完成后释放锁 — 只删除自己创建的锁文件（通过 token 验证）
        try:
            current = _read_lock_data(path)
            if current.get("token") == token:
                os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
