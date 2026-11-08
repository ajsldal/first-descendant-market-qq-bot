"""用户、封禁与权限管理路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..db import BotDB
from ..schemas import BlockRequest, PermissionsUpdate
from ..security import get_current_admin, require_role, verify_csrf
from ..utils import row_dict, rows_dict

router = APIRouter(prefix="/api/users", tags=["users"])

_READ = Depends(get_current_admin)
_WRITE = Depends(require_role("super_admin", "operator"))


def _db(request: Request) -> BotDB:
    return request.app.state.bot_db


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _audit(
    request: Request,
    admin: dict,
    action: str,
    target: str,
    before=None,
    after=None,
) -> None:
    request.app.state.admin_db.audit(
        int(admin["id"]),
        action,
        target_type="user",
        target_id=target,
        before=before,
        after=after,
        ip=_ip(request),
    )


@router.get("")
def list_users(
    request: Request,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    admin: dict = _READ,
) -> dict:
    rows = _db(request).list_users(query=query, limit=limit, offset=offset)
    return {"items": rows_dict(rows), "limit": limit, "offset": offset}


@router.get("/{qq}")
def get_user(
    qq: int, request: Request, admin: dict = _READ
) -> dict:
    db = _db(request)
    row = db.get_user(qq)
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {
        "user": row_dict(row),
        "permissions": row_dict(db.get_permissions(qq)),
        "subscriptions": rows_dict(db.list_subscriptions(qq=qq, limit=200, offset=0)),
    }


@router.put("/{qq}/permissions")
def update_permissions(
    qq: int,
    payload: PermissionsUpdate,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    db = _db(request)
    before = row_dict(db.get_permissions(qq))
    fields = payload.model_dump(exclude_unset=True)
    db.update_permissions(qq, fields, int(admin["id"]))
    after = row_dict(db.get_permissions(qq))
    _audit(request, admin, "user.permissions.update", str(qq), before, after)
    return {"permissions": after}


@router.post("/{qq}/permissions/reset")
def reset_permissions(
    qq: int, request: Request, admin: dict = _WRITE
) -> dict:
    verify_csrf(request)
    db = _db(request)
    before = row_dict(db.get_permissions(qq))
    db.reset_permissions(qq)
    _audit(request, admin, "user.permissions.reset", str(qq), before, None)
    return {"status": "ok"}


@router.post("/{qq}/block")
def block_user(
    qq: int,
    payload: BlockRequest,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    db = _db(request)
    row = db.get_user(qq)
    before = row_dict(row) if row else {"qq": qq}
    db.block_user(qq, reason=payload.reason, until=payload.until, admin_id=int(admin["id"]))
    _audit(
        request,
        admin,
        "user.block",
        str(qq),
        before,
        {"reason": payload.reason, "blocked_until": payload.until},
    )
    return {"status": "ok"}


@router.delete("/{qq}/block")
def unblock_user(
    qq: int, request: Request, admin: dict = _WRITE
) -> dict:
    verify_csrf(request)
    db = _db(request)
    before = row_dict(db.get_user(qq))
    db.unblock_user(qq)
    _audit(request, admin, "user.unblock", str(qq), before, None)
    return {"status": "ok"}


@router.get("/{qq}/subscriptions")
def user_subscriptions(
    qq: int, request: Request, admin: dict = _READ
) -> dict:
    rows = _db(request).list_subscriptions(qq=qq, limit=200, offset=0)
    return {"items": rows_dict(rows)}
