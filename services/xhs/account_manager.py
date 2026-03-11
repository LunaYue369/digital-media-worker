"""小红书多账号管理器

管理多个小红书账号，每个账号使用独立的 Chrome profile 目录：
- 每个账号有自己的 user-data-dir，实现 cookie / 登录态隔离
- 账号信息存储在 JSON 配置文件中（accounts.json）
- 支持 添加 / 删除 / 列表 / 切换默认账号 等操作

为什么需要多账号？
- 如果你只有一个小红书号，用 "default" 就够了
- 如果要管理多个小红书号（比如不同门店），每个号需要独立的 Chrome profile
  这样每个账号的登录 cookie 互不干扰

CLI 用法（手动管理账号时使用）：
    python account_manager.py list                        # 列出所有账号
    python account_manager.py add <名称> [--alias <别名>]  # 添加新账号
    python account_manager.py remove <名称>                # 删除账号
    python account_manager.py info <名称>                  # 查看账号详情
    python account_manager.py set-default <名称>           # 设置默认账号
"""

import json
import os
import sys
import shutil
from typing import Optional

# ── 配置文件路径 ──
# accounts.json 存放在 xhs 模块内的 config/ 目录下
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")

# ── Chrome profile 基础目录 ──
# 每个账号在此目录下有一个子文件夹，存放独立的 Chrome 用户数据（cookie、缓存等）
# Windows 示例：C:\Users\xxx\AppData\Local\Google\Chrome\XiaohongshuProfiles\default\
PROFILES_BASE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                              "Google", "Chrome", "XiaohongshuProfiles")

# 默认账号名称（只有一个小红书号时使用）
DEFAULT_PROFILE_NAME = "default"


def _ensure_config_dir():
    """确保配置文件目录存在，不存在则自动创建"""
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _load_accounts() -> dict:
    """从 accounts.json 加载账号配置

    如果文件不存在或解析失败，返回包含一个 "default" 账号的默认配置。
    """
    _ensure_config_dir()
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    # 文件不存在时返回默认配置（只有一个 "default" 账号）
    return {
        "default_account": DEFAULT_PROFILE_NAME,
        "accounts": {
            DEFAULT_PROFILE_NAME: {
                "alias": "默认账号",
                "profile_dir": os.path.join(PROFILES_BASE, DEFAULT_PROFILE_NAME),
                "created_at": None,
            }
        }
    }


def _save_accounts(data: dict):
    """将账号配置保存到 accounts.json"""
    _ensure_config_dir()
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_profile_dir(account_name: Optional[str] = None) -> str:
    """获取指定账号的 Chrome profile 目录路径

    每个账号有独立的 profile 目录，Chrome 启动时通过 --user-data-dir 指定，
    这样不同账号的 cookie / 登录态 / 缓存完全隔离。

    Args:
        account_name: 账号名称。传 None 则使用默认账号。

    Returns:
        该账号的 Chrome user-data-dir 路径
    """
    data = _load_accounts()

    if account_name is None:
        account_name = data.get("default_account", DEFAULT_PROFILE_NAME)

    if account_name not in data["accounts"]:
        # 账号不存在，回退到默认账号
        account_name = DEFAULT_PROFILE_NAME
        if account_name not in data["accounts"]:
            # 默认账号也不存在，自动创建一个
            data["accounts"][account_name] = {
                "alias": "默认账号",
                "profile_dir": os.path.join(PROFILES_BASE, account_name),
                "created_at": None,
            }
            _save_accounts(data)

    return data["accounts"][account_name]["profile_dir"]


def get_default_account() -> str:
    """获取当前默认账号的名称"""
    data = _load_accounts()
    return data.get("default_account", DEFAULT_PROFILE_NAME)


def set_default_account(account_name: str) -> bool:
    """设置默认账号

    Returns:
        True 设置成功，False 账号不存在
    """
    data = _load_accounts()
    if account_name not in data["accounts"]:
        return False
    data["default_account"] = account_name
    _save_accounts(data)
    return True


def list_accounts() -> list[dict]:
    """列出所有已注册的账号

    Returns:
        账号信息列表，每项包含 name / alias / profile_dir / is_default
    """
    data = _load_accounts()
    default = data.get("default_account", DEFAULT_PROFILE_NAME)
    result = []
    for name, info in data["accounts"].items():
        result.append({
            "name": name,
            "alias": info.get("alias", ""),
            "profile_dir": info.get("profile_dir", ""),
            "is_default": name == default,
        })
    return result


def add_account(name: str, alias: Optional[str] = None) -> bool:
    """添加一个新账号

    会自动创建对应的 Chrome profile 目录。
    添加后需要手动运行 login 命令扫码登录。

    Args:
        name: 账号唯一标识（如 "store_beijing"）
        alias: 显示名称 / 备注（如 "北京旗舰店"）

    Returns:
        True 添加成功，False 名称已存在
    """
    data = _load_accounts()
    if name in data["accounts"]:
        return False

    from datetime import datetime
    profile_dir = os.path.join(PROFILES_BASE, name)
    os.makedirs(profile_dir, exist_ok=True)

    data["accounts"][name] = {
        "alias": alias or name,
        "profile_dir": profile_dir,
        "created_at": datetime.now().isoformat(),
    }
    _save_accounts(data)
    return True


def remove_account(name: str, delete_profile: bool = False) -> bool:
    """删除一个账号

    Args:
        name: 要删除的账号名称
        delete_profile: 是否同时删除 Chrome profile 目录（包括 cookie 等数据）

    Returns:
        True 删除成功，False 账号不存在或是唯一的默认账号（不允许删除）
    """
    data = _load_accounts()
    if name not in data["accounts"]:
        return False

    # 如果是唯一的账号且是默认账号，不允许删除
    if name == data.get("default_account") and len(data["accounts"]) == 1:
        return False

    profile_dir = data["accounts"][name].get("profile_dir", "")
    del data["accounts"][name]

    # 如果删除的是默认账号，自动将第一个剩余账号设为默认
    if name == data.get("default_account"):
        data["default_account"] = next(iter(data["accounts"].keys()))

    _save_accounts(data)

    # 按需删除 Chrome profile 目录
    if delete_profile and profile_dir and os.path.isdir(profile_dir):
        try:
            shutil.rmtree(profile_dir)
        except Exception:
            pass

    return True


def get_account_info(name: str) -> Optional[dict]:
    """获取指定账号的详细信息

    Returns:
        账号信息字典，不存在则返回 None
    """
    data = _load_accounts()
    if name not in data["accounts"]:
        return None
    info = data["accounts"][name].copy()
    info["name"] = name
    info["is_default"] = name == data.get("default_account")
    return info


def account_exists(name: str) -> bool:
    """检查账号是否存在"""
    data = _load_accounts()
    return name in data["accounts"]


# ---------------------------------------------------------------------------
# CLI 命令行工具
# ---------------------------------------------------------------------------
# 以下代码只在直接运行 `python account_manager.py <命令>` 时执行
# 用于手动管理账号，Bot 运行时不会走到这里

def main():
    import argparse

    parser = argparse.ArgumentParser(description="小红书账号管理器")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── list: 列出所有账号 ──
    sub.add_parser("list", help="列出所有账号")

    # ── add: 添加新账号 ──
    p_add = sub.add_parser("add", help="添加新账号")
    p_add.add_argument("name", help="账号唯一标识")
    p_add.add_argument("--alias", help="显示名称 / 备注")

    # ── remove: 删除账号 ──
    p_rm = sub.add_parser("remove", help="删除账号")
    p_rm.add_argument("name", help="要删除的账号名称")
    p_rm.add_argument("--delete-profile", action="store_true",
                      help="同时删除 Chrome profile 目录")

    # ── info: 查看账号详情 ──
    p_info = sub.add_parser("info", help="查看账号详情")
    p_info.add_argument("name", help="账号名称")

    # ── set-default: 设置默认账号 ──
    p_def = sub.add_parser("set-default", help="设置默认账号")
    p_def.add_argument("name", help="要设为默认的账号名称")

    # ── get-profile-dir: 获取 profile 目录路径（内部使用） ──
    p_dir = sub.add_parser("get-profile-dir", help="获取账号的 profile 目录路径")
    p_dir.add_argument("--account", help="账号名称（默认：默认账号）")

    args = parser.parse_args()

    if args.command == "list":
        accounts = list_accounts()
        if not accounts:
            print("暂无已配置的账号。")
            return
        print(f"{'名称':<20} {'别名':<20} {'默认':<10}")
        print("-" * 50)
        for acc in accounts:
            default_mark = "*" if acc["is_default"] else ""
            print(f"{acc['name']:<20} {acc['alias']:<20} {default_mark:<10}")

    elif args.command == "add":
        if add_account(args.name, args.alias):
            print(f"已添加账号「{args.name}」。")
            print(f"Profile 目录：{get_profile_dir(args.name)}")
            print("\n首次登录请运行：")
            print(f"  python cdp_publish.py --account {args.name} login")
        else:
            print(f"错误：账号「{args.name}」已存在。", file=sys.stderr)
            sys.exit(1)

    elif args.command == "remove":
        if remove_account(args.name, args.delete_profile):
            print(f"已删除账号「{args.name}」。")
        else:
            print(f"错误：无法删除账号「{args.name}」。", file=sys.stderr)
            sys.exit(1)

    elif args.command == "info":
        info = get_account_info(args.name)
        if info:
            print(f"名称：{info['name']}")
            print(f"别名：{info.get('alias', '')}")
            print(f"Profile 目录：{info.get('profile_dir', '')}")
            print(f"默认账号：{'是' if info.get('is_default') else '否'}")
            print(f"创建时间：{info.get('created_at', '未知')}")
        else:
            print(f"错误：账号「{args.name}」不存在。", file=sys.stderr)
            sys.exit(1)

    elif args.command == "set-default":
        if set_default_account(args.name):
            print(f"已将默认账号设为「{args.name}」。")
        else:
            print(f"错误：账号「{args.name}」不存在。", file=sys.stderr)
            sys.exit(1)

    elif args.command == "get-profile-dir":
        print(get_profile_dir(args.account))


if __name__ == "__main__":
    main()
