"""后台管理服务配置。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


def _resolve_path(value: str | os.PathLike[str], default: str) -> Path:
    raw = Path(value or default)
    return raw if raw.is_absolute() else ROOT / raw


def _raw_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_host() -> str:
    host = os.getenv("ADMIN_HOST") or _raw_config().get("admin_host") or "127.0.0.1"
    return str(host).strip() or "127.0.0.1"


def _resolve_port() -> int:
    for value in (os.getenv("ADMIN_PORT"), _raw_config().get("admin_port")):
        try:
            port = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return 8090


@dataclass(frozen=True)
class AdminSettings:
    bot_db: Path = field(
        default_factory=lambda: _resolve_path(os.getenv("QQBOT_DB", ""), "data/qqbot.db")
    )
    admin_db: Path = field(
        default_factory=lambda: _resolve_path(os.getenv("ADMIN_DB", ""), "data/admin.db")
    )
    host: str = field(default_factory=_resolve_host)
    port: int = field(default_factory=_resolve_port)
    session_cookie: str = "tfd_admin_session"
    csrf_cookie: str = "tfd_admin_csrf"
    session_ttl: int = 8 * 3600
    idle_ttl: int = 30 * 60
    secure_cookies: bool = os.getenv("ADMIN_SECURE_COOKIES", "") == "1"
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )


def load_config_defaults() -> dict[str, Any]:
    """把 config.json 的现有值作为 system_settings 的种子默认值。"""
    data = _raw_config()
    defaults: dict[str, Any] = {
        "analytics_enabled": True,
        "analytics_whitelist_qq": [],
        "analytics_runs_retention_days": 30,
        "max_subscriptions": 5,
        "steam_auto_login_enabled": True,
        "steam_auto_allowed_qq": [],
        "steam_auto_guard_timeout": 1800,
        # 登录命令白名单（扫码/自动/导入/状态等），为空则全部禁止
        "login_allowed_qq": [],
        # 好友申请：自动同意 + 欢迎
        "friend_auto_accept_enabled": True,
        "friend_welcome_enabled": True,
        "friend_accept_max_per_min": 5,
        # 登录浏览器代理（Steam 登录需要），地址为空时回退启动参数
        "login_proxy_enabled": True,
        "login_proxy_url": "",
        # 手机推送（Bark）：全局开关与节流
        "bark_enabled": True,
        "bark_cooldown_sec": 120,
        "bark_level": "timeSensitive",
        "bark_group": "TFD市场",
        "bark_call": False,
        "bark_max_per_min": 3,
        # 倒卖利润估算（仅手机订阅）
        "flip_enabled": True,
        "flip_fee": 500,
        "flip_tax_rate": 0.1,
        "flip_cooldown_sec": 600,
        # 成交统计（仅超越武器，2 天小时级）
        "trade_stats_enabled": True,
        "trade_stats_interval_hours": 12,
    }
    if not data:
        return defaults

    if "analytics_enabled" in data:
        defaults["analytics_enabled"] = bool(data["analytics_enabled"])
    whitelist = data.get("analytics_whitelist_qq")
    if isinstance(whitelist, list):
        defaults["analytics_whitelist_qq"] = [
            int(item) for item in whitelist if str(item).strip().isdigit()
        ]
    steam_whitelist = data.get("steam_auto_allowed_qq")
    if isinstance(steam_whitelist, list):
        defaults["steam_auto_allowed_qq"] = [
            int(item) for item in steam_whitelist if str(item).strip().isdigit()
        ]
    login_whitelist = data.get("login_allowed_qq")
    if isinstance(login_whitelist, list):
        defaults["login_allowed_qq"] = [
            int(item) for item in login_whitelist if str(item).strip().isdigit()
        ]
    defaults["steam_auto_login_enabled"] = bool(
        data.get("steam_auto_login_enabled", defaults["steam_auto_login_enabled"])
    )
    defaults["friend_auto_accept_enabled"] = bool(
        data.get("friend_auto_accept_enabled", defaults["friend_auto_accept_enabled"])
    )
    defaults["friend_welcome_enabled"] = bool(
        data.get("friend_welcome_enabled", defaults["friend_welcome_enabled"])
    )
    defaults["login_proxy_enabled"] = bool(
        data.get("login_proxy_enabled", defaults["login_proxy_enabled"])
    )
    if isinstance(data.get("login_proxy_url"), str):
        defaults["login_proxy_url"] = data["login_proxy_url"].strip()
    defaults["bark_enabled"] = bool(data.get("bark_enabled", defaults["bark_enabled"]))
    defaults["bark_call"] = bool(data.get("bark_call", defaults["bark_call"]))
    defaults["flip_enabled"] = bool(data.get("flip_enabled", defaults["flip_enabled"]))
    defaults["trade_stats_enabled"] = bool(
        data.get("trade_stats_enabled", defaults["trade_stats_enabled"])
    )
    if isinstance(data.get("bark_level"), str) and data["bark_level"] in (
        "active", "timeSensitive", "passive", "critical",
    ):
        defaults["bark_level"] = data["bark_level"]
    if isinstance(data.get("bark_group"), str) and data["bark_group"].strip():
        defaults["bark_group"] = data["bark_group"].strip()
    for key, minimum in (
        ("analytics_runs_retention_days", 1),
        ("max_subscriptions", 1),
        ("steam_auto_guard_timeout", 60),
        ("bark_cooldown_sec", 10),
        ("bark_max_per_min", 1),
        ("flip_fee", 0),
        ("flip_cooldown_sec", 10),
        ("trade_stats_interval_hours", 1),
        ("friend_accept_max_per_min", 1),
    ):
        try:
            value = int(data.get(key))
        except (TypeError, ValueError):
            continue
        if value >= minimum:
            defaults[key] = value
    try:
        rate = float(data.get("flip_tax_rate"))
    except (TypeError, ValueError):
        rate = -1.0
    if 0.0 <= rate <= 0.5:
        defaults["flip_tax_rate"] = rate
    return defaults
