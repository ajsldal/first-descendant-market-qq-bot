"""QQ 机器人入口。

用法::

    $env:NAPCAT_TOKEN = "<token>"
    .\\.venv\\Scripts\\python.exe run_bot.py

可选参数：--url / --ws-url / --token / --db / --tick / --workers / --max-pages
         / --image / --no-image / --image-style

可调数量参数（如每个用户的订阅上限）从同目录的 config.json 读取。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from qqbot.bot import BotSettings, QQBot
from qqbot.napcat_client import NapCatError

if os.name == "nt":
    import msvcrt

DEFAULT_URL = "http://127.0.0.1:3000"
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_MAX_SUBSCRIPTIONS = 5


def _load_config(path: Path | None = None) -> dict[str, Any]:
    """读取 config.json；文件缺失或损坏时返回空配置，沿用代码默认值。"""
    target = path or CONFIG_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"配置文件 {target} 读取失败，使用默认值：{exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"配置文件 {target} 顶层必须是 JSON 对象，已忽略。", file=sys.stderr)
        return {}
    return data


def _positive_int(value: Any, default: int) -> int:
    """把配置值转成正整数；非法值回退到默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 1 else default


def _config_flag(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是", "开")


def _parse_whitelist(value: Any) -> frozenset[int]:
    """analytics_whitelist_qq 支持 JSON 数组或逗号分隔字符串。"""
    if isinstance(value, str):
        return frozenset(_parse_ids(value))
    if isinstance(value, (list, tuple, set)):
        ids: set[int] = set()
        for part in value:
            text = str(part).strip()
            if text.isdigit():
                ids.add(int(text))
        return frozenset(ids)
    return frozenset()


def _acquire_lock(path: str):
    """单实例文件锁；进程崩溃时由操作系统自动释放。"""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release_lock(handle) -> None:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def _ws_url(http_url: str) -> str:
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/"


def _parse_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for part in (value or "").replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


_parse_groups = _parse_ids


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TFD 市场 QQ 机器人（NapCat / OneBot v11）")
    p.add_argument("--url", default=os.getenv("NAPCAT_URL", DEFAULT_URL))
    p.add_argument("--ws-url", default=os.getenv("NAPCAT_WS_URL"))
    p.add_argument("--token", default=os.getenv("NAPCAT_TOKEN", ""))
    p.add_argument("--db", default=os.getenv("QQBOT_DB", "data/qqbot.db"))
    p.add_argument("--tick", type=int, default=30, help="调度 tick 秒数")
    p.add_argument("--workers", type=int, default=2, help="并发抓取线程数")
    p.add_argument("--max-pages", type=int, default=3, help="单轮最多翻页数")
    p.add_argument(
        "--groups",
        default=os.getenv("QQBOT_GROUPS", ""),
        help="群白名单，逗号分隔；不填表示允许所有群",
    )
    p.add_argument(
        "--ignore-qq",
        default=os.getenv("QQBOT_IGNORE_QQ", ""),
        help="永不响应的 QQ，逗号分隔；用于屏蔽 QQ管家或其他机器人",
    )
    p.add_argument(
        "--image",
        action=argparse.BooleanOptionalAction,
        default=_env_flag("QQBOT_IMAGE", True),
        help="推送商品图片卡片（默认开启；--no-image 退回纯文字）",
    )
    p.add_argument(
        "--image-style",
        default=os.getenv("QQBOT_IMAGE_STYLE", "hybrid"),
        choices=("official", "compact", "hybrid"),
        help="图片版式：official 官网风 / compact 紧凑 / hybrid 混合（默认）",
    )
    p.add_argument(
        "--login-proxy",
        default=os.getenv("TFD_LOGIN_PROXY", ""),
        help="Steam 扫码登录使用的代理，例如 http://127.0.0.1:7897；不填则直连（国内通常不可用）",
    )
    p.add_argument(
        "--login-timeout",
        type=int,
        default=int(os.getenv("TFD_LOGIN_TIMEOUT", "300")),
        help="扫码登录等待秒数（默认 300）",
    )
    p.add_argument(
        "--login-bypass",
        action=argparse.BooleanOptionalAction,
        default=_env_flag("TFD_LOGIN_BYPASS", True),
        help="登录浏览器里 tfd.nexon.com / tfd-api.nexon.com 直连，避免代理 403（默认开启）",
    )
    p.add_argument(
        "--login-debug",
        action="store_true",
        default=_env_flag("TFD_LOGIN_DEBUG", False),
        help="输出 [audit] 级登录诊断日志",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _setup_logging(verbose: bool) -> None:
    Path("logs").mkdir(exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        "logs/qqbot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    if not args.token:
        print("缺少 token：请设置环境变量 NAPCAT_TOKEN 或传 --token", file=sys.stderr)
        return 2

    config = _load_config()
    max_subscriptions = _positive_int(
        config.get("max_subscriptions"), DEFAULT_MAX_SUBSCRIPTIONS
    )
    analytics_whitelist = _parse_whitelist(config.get("analytics_whitelist_qq"))
    analytics_runs_retention_days = _positive_int(
        config.get("analytics_runs_retention_days"), 30
    )
    steam_auto_login_enabled = _config_flag(
        config.get("steam_auto_login_enabled"), True
    )
    steam_auto_allowed_qq = _parse_whitelist(config.get("steam_auto_allowed_qq"))
    steam_auto_guard_timeout = _positive_int(
        config.get("steam_auto_guard_timeout"), 1800
    )
    login_allowed_qq = _parse_whitelist(config.get("login_allowed_qq"))
    friend_auto_accept_enabled = _config_flag(
        config.get("friend_auto_accept_enabled"), True
    )
    friend_welcome_enabled = _config_flag(
        config.get("friend_welcome_enabled"), True
    )
    friend_accept_max_per_min = _positive_int(
        config.get("friend_accept_max_per_min"), 5
    )

    settings = BotSettings(
        url=args.url,
        ws_url=args.ws_url or _ws_url(args.url),
        token=args.token,
        db_path=args.db,
        tick_seconds=args.tick,
        workers=args.workers,
        max_pages=args.max_pages,
        group_whitelist=frozenset(_parse_ids(args.groups)),
        ignore_qq=frozenset(_parse_ids(args.ignore_qq)),
        image_enabled=args.image,
        image_style=args.image_style,
        max_subscriptions=max_subscriptions,
        analytics_enabled=_config_flag(config.get("analytics_enabled"), True),
        analytics_whitelist=analytics_whitelist,
        analytics_runs_retention_days=analytics_runs_retention_days,
        steam_auto_login_enabled=steam_auto_login_enabled,
        steam_auto_allowed_qq=steam_auto_allowed_qq,
        steam_auto_guard_timeout=steam_auto_guard_timeout,
        login_allowed_qq=login_allowed_qq,
        friend_auto_accept_enabled=friend_auto_accept_enabled,
        friend_welcome_enabled=friend_welcome_enabled,
        friend_accept_max_per_min=friend_accept_max_per_min,
        login_proxy=args.login_proxy,
        login_timeout=args.login_timeout,
        login_bypass=args.login_bypass,
        login_debug=args.login_debug,
    )

    lock = _acquire_lock(settings.db_path + ".lock")
    if lock is None:
        print(
            f"已有实例在运行（锁文件 {settings.db_path}.lock），本次退出。",
            file=sys.stderr,
        )
        return 3

    bot = QQBot(settings)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n已停止。")
    except NapCatError as exc:
        print(f"连接 NapCat 失败：{exc}", file=sys.stderr)
        return 2
    finally:
        _release_lock(lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
