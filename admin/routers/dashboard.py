"""后台首页仪表盘。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from ..security import get_current_admin
from ..utils import rows_dict

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_READ = Depends(get_current_admin)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@router.get("")
def dashboard(request: Request, admin: dict = _READ) -> dict:
    bot_db = request.app.state.bot_db
    admin_db = request.app.state.admin_db
    settings = request.app.state.settings
    stats = bot_db.stats()
    return {
        "stats": stats,
        "dbs": {
            "bot": {
                "path": str(settings.bot_db),
                "size": _size(settings.bot_db),
                "wal": _size(settings.bot_db.with_name(settings.bot_db.name + "-wal")),
            },
            "admin": {
                "path": str(settings.admin_db),
                "size": _size(settings.admin_db),
            },
        },
        "recent_subscriptions": rows_dict(
            bot_db.list_subscriptions(limit=5, offset=0)
        ),
        "recent_audit": rows_dict(admin_db.list_audit(limit=5)),
        "settings": {**request.app.state.config_defaults, **bot_db.get_settings()},
    }
