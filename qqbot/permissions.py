"""从数据库读取后台配置的权限/封禁/订阅分析白名单，30 秒缓存。"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from .store import Store

DEFAULT_FEATURES = {
    "allow_subscribe": True,
    "allow_login": True,
    "allow_trade": True,
    "allow_price_query": True,
}


class PermissionService:
    """admin 服务写入，bot 只读；修改后最多 ttl 秒生效，无需重启。"""

    def __init__(
        self,
        store: Store,
        *,
        defaults: dict[str, Any] | None = None,
        ttl: int = 30,
    ) -> None:
        self.store = store
        self.ttl = max(5, int(ttl))
        self._lock = threading.RLock()
        self._defaults = {
            "max_subscriptions": int((defaults or {}).get("max_subscriptions", 5)),
            "analytics_enabled": bool((defaults or {}).get("analytics_enabled", True)),
            "analytics_whitelist_qq": list((defaults or {}).get("analytics_whitelist_qq") or []),
            "analytics_runs_retention_days": int(
                (defaults or {}).get("analytics_runs_retention_days", 30)
            ),
            "steam_auto_login_enabled": bool(
                (defaults or {}).get("steam_auto_login_enabled", True)
            ),
            "steam_auto_allowed_qq": list(
                (defaults or {}).get("steam_auto_allowed_qq") or []
            ),
            "steam_auto_guard_timeout": int(
                (defaults or {}).get("steam_auto_guard_timeout", 1800)
            ),
            # 全部登录命令的白名单；为空表示所有人禁止
            "login_allowed_qq": list((defaults or {}).get("login_allowed_qq") or []),
            # 好友申请：自动同意与欢迎
            "friend_auto_accept_enabled": bool(
                (defaults or {}).get("friend_auto_accept_enabled", True)
            ),
            "friend_welcome_enabled": bool(
                (defaults or {}).get("friend_welcome_enabled", True)
            ),
            "friend_accept_max_per_min": int(
                (defaults or {}).get("friend_accept_max_per_min", 5)
            ),
            "login_proxy_enabled": bool(
                (defaults or {}).get("login_proxy_enabled", True)
            ),
            "login_proxy_url": str((defaults or {}).get("login_proxy_url") or ""),
            "bark_enabled": bool((defaults or {}).get("bark_enabled", True)),
            "bark_cooldown_sec": int((defaults or {}).get("bark_cooldown_sec", 120)),
            "bark_level": str((defaults or {}).get("bark_level") or "timeSensitive"),
            "bark_group": str((defaults or {}).get("bark_group") or "TFD市场"),
            "bark_call": bool((defaults or {}).get("bark_call", False)),
            "bark_max_per_min": int((defaults or {}).get("bark_max_per_min", 3)),
            "flip_enabled": bool((defaults or {}).get("flip_enabled", True)),
            "flip_fee": int((defaults or {}).get("flip_fee", 500)),
            "flip_tax_rate": float((defaults or {}).get("flip_tax_rate", 0.1)),
            "flip_cooldown_sec": int((defaults or {}).get("flip_cooldown_sec", 600)),
        }
        self._settings_at = 0.0
        self._settings: dict[str, Any] = dict(self._defaults)
        self._users: dict[int, tuple[float, dict[str, Any], int, bool]] = {}

    # ── 系统设置 ────────────────────────────────────────────

    def _load_settings(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if now - self._settings_at <= self.ttl:
                return self._settings
            raw = self.store.get_system_settings()
            settings = dict(self._defaults)
            settings.update(raw)

            try:
                settings["max_subscriptions"] = max(1, int(settings["max_subscriptions"]))
            except (TypeError, ValueError):
                settings["max_subscriptions"] = self._defaults["max_subscriptions"]
            settings["analytics_enabled"] = bool(settings.get("analytics_enabled"))
            whitelist = settings.get("analytics_whitelist_qq") or []
            if isinstance(whitelist, str):
                whitelist = [part for part in whitelist.replace("，", ",").split(",")]
            if isinstance(whitelist, Iterable):
                parsed: set[int] = set()
                for item in whitelist:
                    try:
                        parsed.add(int(str(item).strip()))
                    except (TypeError, ValueError):
                        continue
                settings["analytics_whitelist_qq"] = sorted(parsed)
            else:
                settings["analytics_whitelist_qq"] = []
            try:
                settings["analytics_runs_retention_days"] = max(
                    1, int(settings["analytics_runs_retention_days"])
                )
            except (TypeError, ValueError):
                settings["analytics_runs_retention_days"] = 30

            settings["steam_auto_login_enabled"] = bool(
                settings.get("steam_auto_login_enabled")
            )
            settings["steam_auto_allowed_qq"] = self._parse_ids(
                settings.get("steam_auto_allowed_qq")
            )
            settings["login_allowed_qq"] = self._parse_ids(
                settings.get("login_allowed_qq")
            )
            settings["friend_auto_accept_enabled"] = bool(
                settings.get("friend_auto_accept_enabled")
            )
            settings["friend_welcome_enabled"] = bool(
                settings.get("friend_welcome_enabled")
            )
            try:
                settings["friend_accept_max_per_min"] = min(
                    30, max(1, int(settings["friend_accept_max_per_min"]))
                )
            except (TypeError, ValueError):
                settings["friend_accept_max_per_min"] = 5
            try:
                settings["steam_auto_guard_timeout"] = max(
                    60, int(settings["steam_auto_guard_timeout"])
                )
            except (TypeError, ValueError):
                settings["steam_auto_guard_timeout"] = 1800

            settings["login_proxy_enabled"] = bool(
                settings.get("login_proxy_enabled", True)
            )
            settings["login_proxy_url"] = str(settings.get("login_proxy_url") or "").strip()

            settings["bark_enabled"] = bool(settings.get("bark_enabled"))
            settings["bark_call"] = bool(settings.get("bark_call"))
            settings["bark_level"] = str(settings.get("bark_level") or "timeSensitive")
            if settings["bark_level"] not in ("active", "timeSensitive", "passive", "critical"):
                settings["bark_level"] = "timeSensitive"
            settings["bark_group"] = str(settings.get("bark_group") or "TFD市场")[:40]
            try:
                settings["bark_cooldown_sec"] = min(
                    3600, max(10, int(settings["bark_cooldown_sec"]))
                )
            except (TypeError, ValueError):
                settings["bark_cooldown_sec"] = 120
            try:
                settings["bark_max_per_min"] = min(
                    30, max(1, int(settings["bark_max_per_min"]))
                )
            except (TypeError, ValueError):
                settings["bark_max_per_min"] = 3

            settings["flip_enabled"] = bool(settings.get("flip_enabled"))
            try:
                settings["flip_fee"] = min(100000, max(0, int(settings["flip_fee"])))
            except (TypeError, ValueError):
                settings["flip_fee"] = 500
            try:
                settings["flip_tax_rate"] = min(
                    0.5, max(0.0, float(settings["flip_tax_rate"]))
                )
            except (TypeError, ValueError):
                settings["flip_tax_rate"] = 0.1
            try:
                settings["flip_cooldown_sec"] = min(
                    3600, max(10, int(settings["flip_cooldown_sec"]))
                )
            except (TypeError, ValueError):
                settings["flip_cooldown_sec"] = 600

            self._settings = settings
            self._settings_at = now
            return settings

    @staticmethod
    def _parse_ids(value: Any) -> list[int]:
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        if not isinstance(value, Iterable):
            return []
        parsed: set[int] = set()
        for item in value:
            try:
                parsed.add(int(str(item).strip()))
            except (TypeError, ValueError):
                continue
        return sorted(parsed)

    def max_subscriptions(self, qq: int) -> int:
        """用户级配额优先，未设置时用全局值。"""
        _, _, quota = self._load_user(qq)
        return quota

    def analytics_whitelist(self) -> frozenset[int]:
        settings = self._load_settings()
        return frozenset(int(item) for item in settings["analytics_whitelist_qq"])

    def analytics_enabled(self) -> bool:
        return bool(self._load_settings()["analytics_enabled"])

    def analytics_retention_days(self) -> int:
        return int(self._load_settings()["analytics_runs_retention_days"])

    def steam_auto_login_enabled(self) -> bool:
        return bool(self._load_settings()["steam_auto_login_enabled"])

    def steam_auto_allowed_qq(self) -> frozenset[int]:
        settings = self._load_settings()
        return frozenset(int(item) for item in settings["steam_auto_allowed_qq"])

    def login_allowed_qq(self) -> frozenset[int]:
        settings = self._load_settings()
        return frozenset(int(item) for item in settings["login_allowed_qq"])

    def login_allowed(self, qq: int) -> bool:
        """登录命令白名单；空名单按 fail closed 处理。"""
        return int(qq) in self.login_allowed_qq()

    def friend_auto_accept_enabled(self) -> bool:
        return bool(self._load_settings()["friend_auto_accept_enabled"])

    def friend_welcome_enabled(self) -> bool:
        return bool(self._load_settings()["friend_welcome_enabled"])

    def friend_accept_max_per_min(self) -> int:
        return int(self._load_settings()["friend_accept_max_per_min"])

    def steam_auto_guard_timeout(self) -> int:
        return int(self._load_settings()["steam_auto_guard_timeout"])

    def login_proxy_enabled(self) -> bool:
        return bool(self._load_settings()["login_proxy_enabled"])

    def login_proxy_url(self) -> str:
        return str(self._load_settings()["login_proxy_url"])

    def bark_enabled(self) -> bool:
        return bool(self._load_settings()["bark_enabled"])

    def bark_cooldown_sec(self) -> int:
        return int(self._load_settings()["bark_cooldown_sec"])

    def bark_level(self) -> str:
        return str(self._load_settings()["bark_level"])

    def bark_group(self) -> str:
        return str(self._load_settings()["bark_group"])

    def bark_call(self) -> bool:
        return bool(self._load_settings()["bark_call"])

    def bark_max_per_min(self) -> int:
        return int(self._load_settings()["bark_max_per_min"])

    def flip_enabled(self) -> bool:
        return bool(self._load_settings()["flip_enabled"])

    def flip_fee(self) -> float:
        return float(self._load_settings()["flip_fee"])

    def flip_tax_rate(self) -> float:
        return float(self._load_settings()["flip_tax_rate"])

    def flip_cooldown_sec(self) -> int:
        return int(self._load_settings()["flip_cooldown_sec"])

    # ── 用户权限/封禁 ──────────────────────────────────────

    def _load_user(self, qq: int) -> tuple[dict[str, Any], bool, int]:
        now = time.monotonic()
        with self._lock:
            cached = self._users.get(int(qq))
            if cached and now - cached[0] <= self.ttl:
                return cached[1], cached[2], cached[3]

            settings = self._load_settings()
            perm = self.store.get_user_permissions(int(qq))
            block = self.store.get_blocked_user(int(qq))

            features = dict(DEFAULT_FEATURES)
            quota = int(settings["max_subscriptions"])
            if perm is not None:
                for key in DEFAULT_FEATURES:
                    features[key] = bool(perm[key])
                if perm["max_subscriptions"] is not None:
                    try:
                        quota = max(1, int(perm["max_subscriptions"]))
                    except (TypeError, ValueError):
                        pass

            blocked = False
            if block is not None:
                until = block["blocked_until"]
                blocked = until is None or int(until) > int(time.time())

            self._users[int(qq)] = (now, features, blocked, quota)
            return features, blocked, quota

    def is_blocked(self, qq: int) -> bool:
        _, blocked, _ = self._load_user(qq)
        return blocked

    def features(self, qq: int) -> dict[str, bool]:
        features, _, _ = self._load_user(qq)
        return features

    def invalidate(self, qq: int | None = None) -> None:
        with self._lock:
            if qq is None:
                self._users.clear()
                self._settings_at = 0.0
            else:
                self._users.pop(int(qq), None)
