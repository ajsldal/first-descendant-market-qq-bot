"""Steam 自动登录凭据管理（仅本机后台）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from qqbot.steam_vault import SteamVault, VaultError

from ..schemas import SteamCredentialsUpdate
from ..security import get_current_admin, require_role, verify_csrf

router = APIRouter(prefix="/api/steam", tags=["steam"])

_READ = Depends(get_current_admin)
_WRITE = Depends(require_role("super_admin"))


def _vault() -> SteamVault:
    return SteamVault()


def _settings(request: Request) -> dict:
    db = request.app.state.bot_db
    return {
        **request.app.state.config_defaults,
        **db.get_settings(),
    }


def _status(request: Request) -> dict:
    status = _vault().status()
    settings = _settings(request)
    return {
        **status,
        "enabled": bool(settings.get("steam_auto_login_enabled")),
        "allowed_qq": settings.get("steam_auto_allowed_qq") or [],
        "guard_timeout": int(settings.get("steam_auto_guard_timeout") or 1800),
    }


@router.get("/status")
def status(request: Request, admin: dict = _READ) -> dict:
    return _status(request)


@router.put("/credentials")
def save_credentials(
    payload: SteamCredentialsUpdate,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    before = _vault().status()
    try:
        _vault().save(payload.username, payload.password)
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.app.state.admin_db.audit(
        int(admin["id"]),
        "steam.credentials.update",
        target_type="steam",
        target_id="vault",
        before={"configured": before.get("configured")},
        after={"configured": True},
        ip=request.client.host if request.client else "",
    )
    return _status(request)


@router.delete("/credentials")
def clear_credentials(request: Request, admin: dict = _WRITE) -> dict:
    verify_csrf(request)
    before = _vault().status()
    try:
        _vault().clear()
    except VaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.app.state.admin_db.audit(
        int(admin["id"]),
        "steam.credentials.clear",
        target_type="steam",
        target_id="vault",
        before={"configured": before.get("configured")},
        after={"configured": False},
        ip=request.client.host if request.client else "",
    )
    return _status(request)
