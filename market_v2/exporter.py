from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import CollectStats, CatalogItem

CST = timezone(timedelta(hours=8))
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, max_len: int = 80) -> str:
    cleaned = _ILLEGAL.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "item"
    return cleaned[:max_len]


def build_filename(item_name: str, generated_at: datetime | None = None) -> str:
    ts = (generated_at or datetime.now(CST)).astimezone(CST)
    return f"{ts:%Y-%m-%d}_{safe_filename(item_name)}.json"


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_product(raw: dict[str, Any], locale: str = "cn") -> dict[str, Any]:
    price_raw = raw.get("n4SalePrice")
    try:
        price = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None

    registered = raw.get("dtRegister")
    dt_utc = _parse_utc(registered)
    register_cst = dt_utc.astimezone(CST).isoformat() if dt_utc else None

    return {
        "n8Id": str(raw.get("n8Id") or ""),
        "item_name": raw.get("strItemName") or "",
        "item_type": str(raw.get("strType") or "").lower(),
        "price": price,
        "register_time_utc": registered,
        "register_time_cst": register_cst,
        "seller": raw.get("seller") or {},
        "data": raw.get("data") or {},
    }


def build_document(
    *,
    item: CatalogItem,
    locale: str,
    resolved_filters: dict[str, Any],
    payload: dict[str, Any],
    items: list[dict[str, Any]],
    stats: CollectStats,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    ts = (generated_at or datetime.now(CST)).astimezone(CST)
    request_payload = dict(payload)
    request_payload.pop("_sort", None)
    return {
        "schema_version": 1,
        "generated_at": ts.isoformat(),
        "generated_date": f"{ts:%Y-%m-%d}",
        "item": {
            "name": item.name(locale),
            "item_id": item.item_id,
            "item_type": item.item_type,
        },
        "filters": resolved_filters,
        "request": {
            "endpoint": f"POST https://tfd-api.nexon.com/api/v2/trademarket/{locale}/products",
            "payload": request_payload,
        },
        "stats": {
            **stats.as_dict(),
            "matched": len(items),
        },
        "products": [normalize_product(raw, locale) for raw in items],
    }


def write_document(document: dict[str, Any], output_dir: str | Path, item_name: str) -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.fromisoformat(document["generated_at"])
    path = out_dir / build_filename(item_name, generated_at)
    if path.exists():
        # 同一天对同一物品跑多次时，追加生成时分秒避免覆盖
        base = safe_filename(item_name)
        stamp = f"{generated_at:%H%M%S}"
        path = out_dir / f"{generated_at:%Y-%m-%d}_{base}_{stamp}.json"
        index = 2
        while path.exists():
            path = out_dir / f"{generated_at:%Y-%m-%d}_{base}_{stamp}_{index}.json"
            index += 1
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(document, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path
