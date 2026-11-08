"""系统设置：订阅上限、订阅分析开关/白名单/保留天数。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..config import load_config_defaults
from ..db import BotDB
from ..schemas import SettingsUpdate
from ..security import get_current_admin, require_role, verify_csrf

router = APIRouter(prefix="/api/settings", tags=["settings"])

_READ = Depends(get_current_admin)
_WRITE = Depends(require_role("super_admin", "operator"))


def _db(request: Request) -> BotDB:
    return request.app.state.bot_db


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _allowed_keys() -> set[str]:
    return {
        "analytics_enabled",
        "analytics_whitelist_qq",
        "analytics_runs_retention_days",
        "max_subscriptions",
        "steam_auto_login_enabled",
        "steam_auto_allowed_qq",
        "steam_auto_guard_timeout",
        "login_allowed_qq",
        "friend_auto_accept_enabled",
        "friend_welcome_enabled",
        "friend_accept_max_per_min",
        "login_proxy_enabled",
        "login_proxy_url",
        "bark_enabled",
        "bark_cooldown_sec",
        "bark_level",
        "bark_group",
        "bark_call",
        "bark_max_per_min",
        "flip_enabled",
        "flip_fee",
        "flip_tax_rate",
        "flip_cooldown_sec",
        "trade_stats_enabled",
        "trade_stats_interval_hours",
    }


@router.get("")
def get_settings(request: Request, admin: dict = _READ) -> dict:
    db = _db(request)
    defaults = load_config_defaults()
    values = {**defaults, **db.get_settings()}
    return {"settings": values, "defaults": defaults}


@router.put("")
def update_settings(
    payload: SettingsUpdate,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    db = _db(request)
    updates = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if key in _allowed_keys()
    }
    if "analytics_whitelist_qq" in updates and updates["analytics_whitelist_qq"] is not None:
        updates["analytics_whitelist_qq"] = sorted(set(updates["analytics_whitelist_qq"]))
    if "steam_auto_allowed_qq" in updates and updates["steam_auto_allowed_qq"] is not None:
        updates["steam_auto_allowed_qq"] = sorted(set(updates["steam_auto_allowed_qq"]))
    if "login_allowed_qq" in updates and updates["login_allowed_qq"] is not None:
        updates["login_allowed_qq"] = sorted(set(updates["login_allowed_qq"]))
    before = db.get_settings()
    if updates:
        db.update_settings(updates, int(admin["id"]))
    request.app.state.admin_db.audit(
        int(admin["id"]),
        "settings.update",
        target_type="settings",
        target_id=",".join(sorted(updates)),
        before=before,
        after=updates,
        ip=_ip(request),
    )
    return {"settings": {**load_config_defaults(), **db.get_settings()}}
