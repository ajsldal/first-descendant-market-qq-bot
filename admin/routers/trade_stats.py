"""成交统计：物品列表、单件小时数据、抓取状态与手动刷新。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..security import get_current_admin, require_role, verify_csrf
from ..trade_stats import CST
from ..utils import row_dict

router = APIRouter(prefix="/api/trade-stats", tags=["trade-stats"])

WINDOW_SEC = 2 * 86400

_READ = Depends(get_current_admin)
_WRITE = Depends(require_role("super_admin", "operator"))


def _admin_db(request: Request):
    return request.app.state.admin_db


def _crawler(request: Request):
    return request.app.state.trade_stats


def _audit(request: Request, admin: dict, action: str, after: Any = None) -> None:
    request.app.state.admin_db.audit(
        int(admin["id"]),
        action,
        target_type="trade_stats",
        target_id="all",
        after=after,
        ip=request.client.host if request.client else "",
    )


@router.get("/items")
def list_items(request: Request, admin: dict = _READ) -> dict:
    rows = _admin_db(request).list_trade_stats_items()
    items = []
    for row in rows:
        item = row_dict(row)
        trades = item.get("trades_24h") or 0
        turnover = item.get("turnover_24h") or 0
        item["avg_price_24h"] = round(turnover / trades, 1) if trades else None
        if item.get("last_bucket_at"):
            item["last_bucket_text"] = datetime.fromtimestamp(
                int(item["last_bucket_at"]), CST
            ).strftime("%m-%d %H:%M")
        items.append(item)
    return {"items": items}


@router.get("/status")
def status(request: Request, admin: dict = _READ) -> dict:
    crawler = _crawler(request)
    run = _admin_db(request).latest_trade_stats_run()
    return {
        "enabled": crawler.enabled(),
        "interval_hours": crawler.interval_hours(),
        "last_run": row_dict(run) if run is not None else None,
    }


@router.get("/{item_id}")
def item_detail(item_id: str, request: Request, admin: dict = _READ) -> dict:
    db = _admin_db(request)
    item = db.get_trade_stats_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="该武器还没有统计数据")
    rows = db.list_trade_stats_points(item_id, since=int(time.time()) - WINDOW_SEC)
    points = [
        {
            "bucket_at": int(row["bucket_at"]),
            "date": datetime.fromtimestamp(int(row["bucket_at"]), CST).strftime("%Y-%m-%d %H:%M"),
            "average_price": float(row["average_price"]),
            "trade_count": int(row["trade_count"]),
        }
        for row in rows
    ]
    payload = row_dict(item)
    payload["points"] = points
    payload["first_date"] = points[0]["date"] if points else ""
    payload["last_date"] = points[-1]["date"] if points else ""
    return {"item": payload}


@router.post("/refresh")
async def refresh(request: Request, admin: dict = _WRITE) -> dict:
    verify_csrf(request)
    crawler = _crawler(request)
    summary = await crawler.run_once()
    _audit(request, admin, "trade_stats.refresh", summary)
    return {"ok": True, "summary": summary}
