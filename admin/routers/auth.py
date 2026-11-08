"""认证路由：登录、登出、当前管理员、修改密码、CSRF。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..config import AdminSettings
from ..db import AdminDB
from ..schemas import ChangePasswordRequest, LoginRequest
from ..security import (
    get_current_admin,
    hash_password,
    hash_token,
    login_limiter,
    new_csrf_token,
    new_session_token,
    verify_csrf,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _settings(request: Request) -> AdminSettings:
    return request.app.state.settings


def _admin_db(request: Request) -> AdminDB:
    return request.app.state.admin_db


def _set_cookies(
    response: Response, settings: AdminSettings, token: str, csrf: str
) -> None:
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_ttl,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie,
        csrf,
        max_age=settings.session_ttl,
        httponly=False,
        samesite="strict",
        secure=settings.secure_cookies,
        path="/",
    )


def _clear_cookies(response: Response, settings: AdminSettings) -> None:
    response.delete_cookie(settings.session_cookie, path="/")
    response.delete_cookie(settings.csrf_cookie, path="/")


@router.get("/csrf")
def csrf(request: Request, response: Response) -> dict[str, str]:
    settings = _settings(request)
    token = request.cookies.get(settings.csrf_cookie) or new_csrf_token()
    response.set_cookie(
        settings.csrf_cookie,
        token,
        max_age=settings.session_ttl,
        httponly=False,
        samesite="strict",
        secure=settings.secure_cookies,
        path="/",
    )
    return {"csrf_token": token}


@router.post("/login")
def login(
    payload: LoginRequest, request: Request, response: Response
) -> dict[str, str]:
    settings = _settings(request)
    verify_csrf(request)
    db = _admin_db(request)
    ip = request.client.host if request.client else ""
    key = f"{ip}:{payload.username.strip().lower()}"
    if login_limiter.blocked(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录失败次数过多，请 5 分钟后再试",
        )

    row = db.get_admin_by_username(payload.username)
    if row is None or not verify_password(str(row["password_hash"]), payload.password):
        login_limiter.add_failure(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    if int(row["disabled"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员已禁用")

    login_limiter.clear(key)
    db.mark_login(int(row["id"]))
    token = new_session_token()
    csrf_token = new_csrf_token()
    db.create_session(
        int(row["id"]),
        hash_token(token),
        expires_at=int(time.time()) + settings.session_ttl,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    db.audit(
        int(row["id"]),
        "auth.login",
        target_type="admin",
        target_id=str(row["id"]),
        ip=ip,
    )
    _set_cookies(response, settings, token, csrf_token)
    return {
        "username": str(row["username"]),
        "role": str(row["role"]),
        "csrf_token": csrf_token,
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    admin: dict = Depends(get_current_admin),
) -> dict[str, str]:
    verify_csrf(request)
    settings = _settings(request)
    _admin_db(request).revoke_session(int(admin["session_id"]))
    _admin_db(request).audit(
        int(admin["id"]),
        "auth.logout",
        target_type="admin",
        target_id=str(admin["id"]),
        ip=request.client.host if request.client else "",
    )
    _clear_cookies(response, settings)
    return {"status": "ok"}


@router.get("/me")
def me(admin: dict = Depends(get_current_admin)) -> dict:
    return admin


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    admin: dict = Depends(get_current_admin),
) -> dict[str, str]:
    verify_csrf(request)
    db = _admin_db(request)
    row = db.get_admin(int(admin["id"]))
    if row is None or not verify_password(str(row["password_hash"]), payload.old_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="原密码错误"
        )
    if payload.old_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与原密码相同"
        )
    db.update_password(int(admin["id"]), hash_password(payload.new_password))
    db.revoke_admin_sessions(
        int(admin["id"]), keep_session_id=int(admin["session_id"])
    )
    db.audit(
        int(admin["id"]),
        "auth.change_password",
        target_type="admin",
        target_id=str(admin["id"]),
        ip=request.client.host if request.client else "",
    )
    return {"status": "ok"}
