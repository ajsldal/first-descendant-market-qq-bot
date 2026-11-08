"""后台认证、会话、CSRF 与密码哈希。"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request, status

from .config import AdminSettings
from .db import AdminDB

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, ValueError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def verify_csrf(request: Request) -> None:
    settings: AdminSettings = request.app.state.settings
    cookie = request.cookies.get(settings.csrf_cookie, "")
    header = request.headers.get("X-CSRF-Token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")


def get_current_admin(request: Request) -> dict[str, Any]:
    settings: AdminSettings = request.app.state.settings
    db: AdminDB = request.app.state.admin_db
    raw = request.cookies.get(settings.session_cookie, "")
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    row = db.get_session(hash_token(raw))
    now = int(time.time())
    if row is None or row["revoked_at"] is not None or int(row["expires_at"]) <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已失效")
    if int(row["disabled"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员已禁用")
    if now - int(row["last_seen_at"]) > settings.idle_ttl:
        db.revoke_session(int(row["id"]))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已超时")
    if now - int(row["last_seen_at"]) > 60:
        db.touch_session(int(row["id"]), now)
    return {
        "id": int(row["admin_id"]),
        "username": str(row["username"]),
        "role": str(row["role"]),
        "session_id": int(row["id"]),
    }


def require_role(*roles: str) -> Callable[[Request], dict[str, Any]]:
    def dependency(request: Request) -> dict[str, Any]:
        admin = get_current_admin(request)
        if roles and admin["role"] not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")
        return admin

    return dependency


class LoginRateLimiter:
    """内存级登录失败限速；后台是单进程，够用。"""

    def __init__(self, *, max_failures: int = 5, window: int = 300) -> None:
        self.max_failures = max_failures
        self.window = window
        self._lock = threading.Lock()
        self._events: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        events = [t for t in self._events.get(key, []) if now - t < self.window]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            return len(self._recent(key, now)) >= self.max_failures

    def add_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._events.setdefault(key, []).append(now)
            self._recent(key, now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


login_limiter = LoginRateLimiter()


def ensure_bootstrap_admin(
    db: AdminDB,
    *,
    username: str | None = None,
    password: str | None = None,
) -> tuple[str, str] | None:
    """首次启动创建超级管理员；返回 (用户名, 自动生成的密码) 供控制台展示。"""
    if db.count_admins() > 0:
        return None
    name = (username or os.getenv("ADMIN_INITIAL_USERNAME") or "admin").strip()
    raw = password or os.getenv("ADMIN_INITIAL_PASSWORD")
    generated = not raw
    if not raw:
        raw = secrets.token_urlsafe(12)
    db.create_admin(name, hash_password(raw), role="super_admin")
    return name, raw if generated else ""
