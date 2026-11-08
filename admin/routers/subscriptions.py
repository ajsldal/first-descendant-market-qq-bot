"""订阅管理路由（仅后台可见）。"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from qqbot.dsl import DslError
from qqbot.flip import overrides_to_text, parse_flip_overrides

from ..db import BotDB
from ..schemas import SubscriptionUpdate
from ..security import get_current_admin, require_role, verify_csrf
from ..utils import row_dict, rows_dict

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

_READ = Depends(get_current_admin)
_WRITE = Depends(require_role("super_admin", "operator"))


def _db(request: Request) -> BotDB:
    return request.app.state.bot_db


def _audit(request: Request, admin: dict, action: str, sub_id: int, before, after) -> None:
    request.app.state.admin_db.audit(
        int(admin["id"]),
        action,
        target_type="subscription",
        target_id=str(sub_id),
        before=before,
        after=after,
        ip=request.client.host if request.client else "",
    )


def _decorate(item: dict) -> dict:
    """把 flip_overrides_json 转成可直接编辑的 DSL 文本。"""
    raw = item.get("flip_overrides_json") or ""
    overrides: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                overrides = parsed
        except (json.JSONDecodeError, TypeError):
            overrides = {}
    item["flip_overrides_text"] = overrides_to_text(overrides)
    return item


@router.get("")
def list_subscriptions(
    request: Request,
    qq: int | None = None,
    enabled: int | None = None,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    admin: dict = _READ,
) -> dict:
    rows = _db(request).list_subscriptions(
        qq=qq, enabled=enabled, query=query, limit=limit, offset=offset
    )
    items = [_decorate(item) for item in rows_dict(rows)]
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{sub_id}")
def get_subscription(
    sub_id: int, request: Request, admin: dict = _READ
) -> dict:
    row = _db(request).get_subscription(sub_id)
    if row is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"subscription": _decorate(row_dict(row))}


@router.patch("/{sub_id}")
def update_subscription(
    sub_id: int,
    payload: SubscriptionUpdate,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    db = _db(request)
    current = db.get_subscription(sub_id)
    if current is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    updates = payload.model_dump(exclude_unset=True)
    if "flip_overrides" in updates:
        raw = updates.pop("flip_overrides") or ""
        try:
            overrides = parse_flip_overrides(raw)
        except DslError as exc:
            raise HTTPException(
                status_code=400, detail=f"模糊条件格式错误：{exc}"
            ) from exc
        updates["flip_overrides_json"] = (
            json.dumps(overrides, ensure_ascii=False) if overrides else ""
        )
    if not db.update_subscription(sub_id, updates):
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    after = db.get_subscription(sub_id)
    audit_updates = dict(updates)
    _audit(request, admin, "subscription.update", sub_id, row_dict(current), audit_updates)
    return {"subscription": _decorate(row_dict(after))}


@router.post("/{sub_id}/pause")
def pause_subscription(
    sub_id: int, request: Request, admin: dict = _WRITE
) -> dict:
    verify_csrf(request)
    db = _db(request)
    current = db.get_subscription(sub_id)
    if current is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    db.update_subscription(sub_id, {"enabled": False})
    _audit(request, admin, "subscription.pause", sub_id, row_dict(current), {"enabled": False})
    return {"status": "ok"}


@router.post("/{sub_id}/resume")
def resume_subscription(
    sub_id: int, request: Request, admin: dict = _WRITE
) -> dict:
    verify_csrf(request)
    db = _db(request)
    current = db.get_subscription(sub_id)
    if current is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    updates = {"enabled": True, "next_run_at": int(time.time()) + 5}
    db.update_subscription(sub_id, updates)
    _audit(request, admin, "subscription.resume", sub_id, row_dict(current), updates)
    return {"status": "ok"}


@router.delete("/{sub_id}")
def delete_subscription(
    sub_id: int, request: Request, admin: dict = _WRITE
) -> dict:
    verify_csrf(request)
    db = _db(request)
    current = db.get_subscription(sub_id)
    if current is None:
        raise HTTPException(status_code=404, detail="订阅不存在")
    db.delete_subscription(sub_id)
    _audit(request, admin, "subscription.delete", sub_id, row_dict(current), None)
    return {"status": "ok"}
