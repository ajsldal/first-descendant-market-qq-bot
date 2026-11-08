"""Bark 手机推送配置（每用户一份，Key 加密存储、只回显末四位）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from qqbot.bark import (
    BarkError,
    build_payload,
    normalize_sound,
    parse_bark_target,
    post_bark,
    protect_key,
    unprotect_key,
)

from ..config import load_config_defaults
from ..db import BotDB
from ..schemas import BarkToggle, BarkUpdate
from ..security import get_current_admin, require_role, verify_csrf

router = APIRouter(prefix="/api/users", tags=["bark"])

_READ = Depends(get_current_admin)
_WRITE = Depends(require_role("super_admin", "operator"))


def _db(request: Request) -> BotDB:
    return request.app.state.bot_db


def _audit(request: Request, admin: dict, action: str, qq: int, before=None, after=None) -> None:
    request.app.state.admin_db.audit(
        int(admin["id"]),
        action,
        target_type="user_bark",
        target_id=str(qq),
        before=before,
        after=after,
        ip=request.client.host if request.client else "",
    )


def _status(row, *, include_error: bool = True) -> dict:
    if row is None:
        return {
            "configured": False,
            "enabled": False,
            "server": "",
            "sound": "",
            "key_tail": "",
            "last_sent_at": 0,
            "last_error": "",
        }
    tail = ""
    try:
        tail = "****" + unprotect_key(row["device_key_enc"])[-4:]
    except (BarkError, TypeError):
        tail = "****"
    return {
        "configured": True,
        "enabled": bool(row["enabled"]),
        "server": str(row["server"] or ""),
        "sound": str(row["sound"] or ""),
        "key_tail": tail,
        "last_sent_at": int(row["last_sent_at"] or 0),
        "last_error": str(row["last_error"] or "") if include_error else "",
    }


@router.get("/{qq}/bark")
def get_user_bark(qq: int, request: Request, admin: dict = _READ) -> dict:
    return _status(_db(request).get_user_bark(qq))


@router.put("/{qq}/bark")
def save_user_bark(
    qq: int,
    payload: BarkUpdate,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    db = _db(request)
    before = _status(db.get_user_bark(qq))
    target = (payload.target or "").strip()

    try:
        sound = normalize_sound(payload.sound) if payload.sound else ""
    except BarkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if target:
        try:
            server, key = parse_bark_target(target)
            encrypted = protect_key(key)
        except BarkError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.save_user_bark(
            qq,
            server=server,
            device_key_enc=encrypted,
            sound=sound,
            enabled=True if payload.enabled is None else bool(payload.enabled),
        )
    else:
        # 不改 Key：只允许改铃声/开关
        if not before["configured"]:
            raise HTTPException(status_code=400, detail="请先填写 Bark 地址或 Key")
        if sound:
            db.set_user_bark_sound(qq, sound)
        if payload.enabled is not None:
            db.set_user_bark_enabled(qq, bool(payload.enabled))

    after = _status(db.get_user_bark(qq))
    _audit(
        request, admin, "bark.update", qq,
        before={"configured": before["configured"], "server": before["server"]},
        after={"configured": True, "server": after["server"], "sound": after["sound"]},
    )
    return after


@router.delete("/{qq}/bark")
def delete_user_bark(qq: int, request: Request, admin: dict = _WRITE) -> dict:
    verify_csrf(request)
    exists = _db(request).delete_user_bark(qq)
    _audit(request, admin, "bark.delete", qq, before={"configured": exists}, after=None)
    return {"configured": False, "deleted": exists}


@router.post("/{qq}/bark/toggle")
def toggle_user_bark(
    qq: int,
    payload: BarkToggle,
    request: Request,
    admin: dict = _WRITE,
) -> dict:
    verify_csrf(request)
    db = _db(request)
    if db.get_user_bark(qq) is None:
        raise HTTPException(status_code=404, detail="该用户还没有配置 Bark")
    db.set_user_bark_enabled(qq, bool(payload.enabled))
    _audit(
        request, admin, "bark.toggle", qq, after={"enabled": bool(payload.enabled)}
    )
    return _status(db.get_user_bark(qq))


@router.post("/{qq}/bark/test")
def test_user_bark(
    qq: int, request: Request, mode: str = "normal", admin: dict = _WRITE
) -> dict:
    """mode=normal 普通测试；mode=call 循环轰炸（call=1，铃声循环 30 秒）。"""
    verify_csrf(request)
    db = _db(request)
    row = db.get_user_bark(qq)
    if row is None:
        raise HTTPException(status_code=404, detail="该用户还没有配置 Bark")
    try:
        key = unprotect_key(row["device_key_enc"])
        sound = normalize_sound(str(row["sound"] or ""))
    except BarkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    flood = mode == "call"
    settings = {**load_config_defaults(), **_db(request).get_settings()}
    payload = build_payload(
        sub_id=0,
        matched=1,
        new_count=1,
        command="/购买 怪物猎人 类型=超越武器 品质=400-400 价格=950-950",
        item_name="怪物猎人（循环轰炸测试）" if flood else "怪物猎人",
        price="950",
        sound=sound,
        group=str(settings.get("bark_group") or "TFD市场"),
        level="timeSensitive" if flood else str(settings.get("bark_level") or "timeSensitive"),
        call=True if flood else bool(settings.get("bark_call")),
    )
    ok, error = post_bark(str(row["server"]), key, payload)
    _audit(request, admin, "bark.test", qq, after={"ok": ok, "mode": "call" if flood else "normal"})
    if not ok:
        raise HTTPException(status_code=502, detail=f"推送失败：{error}")
    return {"ok": True, "sent_at": int(time.time()), "mode": "call" if flood else "normal"}
