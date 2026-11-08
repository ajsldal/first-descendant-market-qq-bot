"""分析数据只读接口：消失历史、同一商品分组、卖家与导狗记录。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..security import get_current_admin
from ..utils import row_dict, rows_dict

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_READ = Depends(get_current_admin)


def _db(request: Request):
    return request.app.state.bot_db


@router.get("/disappeared")
def disappeared(
    request: Request,
    item_id: str = "",
    identity_key: str = "",
    limit: int = 50,
    offset: int = 0,
    admin: dict = _READ,
) -> dict:
    rows = _db(request).list_disappeared(
        item_id=item_id or None,
        identity_key=identity_key or None,
        limit=limit,
        offset=offset,
    )
    return {"items": rows_dict(rows), "limit": limit, "offset": offset}


@router.get("/identity-groups")
def identity_groups(
    request: Request,
    item_id: str = "",
    identity_key: str = "",
    min_sellers: int = Query(default=2, ge=0, le=100),
    limit: int = 50,
    offset: int = 0,
    admin: dict = _READ,
) -> dict:
    rows = _db(request).list_identity_groups(
        item_id=item_id or None,
        identity_key=identity_key or None,
        min_sellers=min_sellers,
        limit=limit,
        offset=offset,
    )
    return {"items": rows_dict(rows), "limit": limit, "offset": offset}


@router.get("/identity-detail")
def identity_detail(
    request: Request,
    item_id: str,
    identity_key: str,
    admin: dict = _READ,
) -> dict:
    db = _db(request)
    group = db.get_identity_group(item_id, identity_key)
    if group is None:
        raise HTTPException(status_code=404, detail="该商品身份还没有分析数据")
    payload = row_dict(group)
    payload.update(db.get_identity_attributes(item_id, identity_key))
    payload["seller_list"] = rows_dict(db.list_group_sellers(item_id, identity_key))
    payload["flips"] = rows_dict(
        db.list_seller_flips(item_id=item_id, identity_key=identity_key, limit=100)
    )
    payload["history"] = rows_dict(
        db.list_identity_history(item_id, identity_key, limit=100)
    )
    return {"item": payload}


@router.get("/group-sellers")
def group_sellers(
    request: Request,
    item_id: str,
    identity_key: str,
    admin: dict = _READ,
) -> dict:
    rows = _db(request).list_group_sellers(item_id, identity_key)
    return {"items": rows_dict(rows)}


@router.get("/flips")
def flips(
    request: Request,
    item_id: str = "",
    seller: str = "",
    identity_key: str = "",
    limit: int = 50,
    offset: int = 0,
    admin: dict = _READ,
) -> dict:
    rows = _db(request).list_seller_flips(
        item_id=item_id or None,
        seller=seller or None,
        identity_key=identity_key or None,
        limit=limit,
        offset=offset,
    )
    return {"items": rows_dict(rows), "limit": limit, "offset": offset}
