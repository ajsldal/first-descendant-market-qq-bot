"""FastAPI 应用工厂。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import ROOT, AdminSettings, load_config_defaults
from .db import AdminDB, BotDB
from .routers import (
    analytics as analytics_router,
    audit as audit_router,
    auth as auth_router,
    bark as bark_router,
    dashboard as dashboard_router,
    settings as settings_router,
    steam as steam_router,
    subscriptions as subscriptions_router,
    trade_stats as trade_stats_router,
    users as users_router,
)
from .security import ensure_bootstrap_admin
from .trade_stats import TradeStatsCrawler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    crawler: TradeStatsCrawler = app.state.trade_stats
    crawler.start()
    try:
        yield
    finally:
        await crawler.stop()


def create_app(settings: AdminSettings | None = None) -> FastAPI:
    settings = settings or AdminSettings()
    admin_db = AdminDB(settings.admin_db)
    bot_db = BotDB(settings.bot_db)
    defaults = load_config_defaults()
    bot_db.seed_settings(defaults)
    admin_db.purge_expired_sessions()

    created = ensure_bootstrap_admin(admin_db)
    if created:
        username, password = created
        if password:
            logger.warning(
                "已创建初始管理员 %s，初始密码：%s（仅显示一次，请尽快修改）",
                username,
                password,
            )
        else:
            logger.warning("已创建初始管理员 %s（密码来自 ADMIN_INITIAL_PASSWORD）", username)

    app = FastAPI(
        title="TFD 市场后台",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.admin_db = admin_db
    app.state.bot_db = bot_db
    app.state.config_defaults = defaults
    app.state.trade_stats = TradeStatsCrawler(admin_db, bot_db)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # Windows 的 mimetypes 可能把 .js 识别成 text/plain；
        # 在 nosniff 下浏览器会拒绝执行，所以这里强制纠正。
        path = request.url.path.lower()
        content_type = response.headers.get("content-type", "")
        if path.endswith((".js", ".mjs")) and "javascript" not in content_type:
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        elif path.endswith(".css") and "text/css" not in content_type:
            response.headers["content-type"] = "text/css; charset=utf-8"
        elif path.endswith((".json", ".map")) and "application/json" not in content_type:
            response.headers["content-type"] = "application/json; charset=utf-8"
        if path == "/" or path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self' http://127.0.0.1:5173 http://localhost:5173"
        )
        return response

    app.include_router(auth_router.router)
    app.include_router(users_router.router)
    app.include_router(bark_router.router)
    app.include_router(settings_router.router)
    app.include_router(steam_router.router)
    app.include_router(subscriptions_router.router)
    app.include_router(analytics_router.router)
    app.include_router(trade_stats_router.router)
    app.include_router(audit_router.router)
    app.include_router(dashboard_router.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "tfd-admin"}

    dist = ROOT / "web-admin" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")

    return app
