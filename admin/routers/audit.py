"""审计日志路由。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request

from ..security import get_current_admin
from ..utils import rows_dict

router = APIRouter(prefix="/api/audit", tags=["audit"])

_READ = Depends(get_current_admin)


def _parse(value: str) -> object | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


@router.get("")
def list_audit(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    action: str = "",
    admin: dict = _READ,
) -> dict:
    rows = request.app.state.admin_db.list_audit(
        limit=limit, offset=offset, action=action or None
    )
    items = rows_dict(rows)
    for item in items:
        item["before"] = _parse(str(item.pop("before_json", "")))
        item["after"] = _parse(str(item.pop("after_json", "")))
    return {"items": items, "limit": limit, "offset": offset}
