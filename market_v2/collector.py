from __future__ import annotations

import copy
import logging
from typing import Any, Protocol

from .models import (
    MAX_PAGES,
    MAX_PRICE,
    MIN_PRICE,
    PAGE_SIZE,
    TOTAL_COUNT_CAP,
    CollectResult,
    CollectStats,
    Page,
)

logger = logging.getLogger(__name__)

MAX_REQUESTS = 20000


class ProductsClient(Protocol):
    def query_products(self, payload: dict[str, Any]) -> Page: ...


def _price_of(item: dict[str, Any]) -> float:
    try:
        return float(item.get("n4SalePrice") or 0)
    except (TypeError, ValueError):
        return 0.0


def _sort_items(items: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "price_desc":
        return sorted(items, key=lambda it: (-_price_of(it), str(it.get("n8Id"))))
    if sort == "price_asc":
        return sorted(items, key=lambda it: (_price_of(it), str(it.get("n8Id"))))
    # latest：dtRegister 是 UTC ISO，字符串排序等价于时间排序
    return sorted(items, key=lambda it: (str(it.get("dtRegister") or ""), str(it.get("n8Id"))), reverse=True)


def collect(
    client: ProductsClient,
    base_payload: dict[str, Any],
    *,
    price_min: int | None = None,
    price_max: int | None = None,
    sort: str = "latest",
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    total_count_cap: int = TOTAL_COUNT_CAP,
) -> CollectResult:
    """按条件抓取全部挂单。

    服务端 totalCount 封顶 10000（500 页）。当某个价格区间的 totalCount 达到封顶值时，
    自动对半切分价格区间递归抓取，保证不遗漏。
    """
    lo = price_min if price_min is not None else MIN_PRICE
    hi = price_max if price_max is not None else MAX_PRICE
    lo = max(int(lo), MIN_PRICE)
    hi = min(int(hi), MAX_PRICE)

    stats = CollectStats()
    collected: dict[str, dict[str, Any]] = {}

    if lo > hi:
        return CollectResult(items=[], stats=stats)

    stack: list[tuple[int, int]] = [(lo, hi)]

    while stack:
        if stats.requests >= MAX_REQUESTS:
            logger.warning("达到请求上限 %s，提前结束", MAX_REQUESTS)
            stats.truncated = True
            break

        slice_lo, slice_hi = stack.pop()
        payload = copy.deepcopy(base_payload)
        payload["pageInfo"] = {"pageNumber": 1, "pageSize": page_size}
        payload["n4SalePriceMin"] = slice_lo
        payload["n4SalePriceMax"] = slice_hi

        page = client.query_products(payload)
        stats.requests += 1
        stats.pages += 1
        total = page.total_count or 0
        stats.server_totals.append(page.total_count)

        if total <= 0:
            continue

        # 封顶：先切分价格区间，避免只拿到前 500 页
        if total >= total_count_cap and slice_lo < slice_hi:
            mid = (slice_lo + slice_hi) // 2
            stack.append((slice_lo, mid))
            stack.append((mid + 1, slice_hi))
            logger.info(
                "价格区间 %s-%s 命中封顶 %s，切分为 %s-%s / %s-%s",
                slice_lo, slice_hi, total, slice_lo, mid, mid + 1, slice_hi,
            )
            continue

        if total >= total_count_cap and slice_lo == slice_hi:
            stats.truncated = True
            logger.warning(
                "价格 %s 单点仍有 %s 条，无法继续切分，只能取前 %s 页",
                slice_lo, total, max_pages,
            )

        stats.slices += 1

        def consume(items: list[dict[str, Any]]) -> int:
            added = 0
            for raw in items:
                key = str(raw.get("n8Id") or "")
                if not key:
                    continue
                if key not in collected:
                    collected[key] = raw
                    added += 1
            return added

        slice_collected = consume(page.items)
        page_number = 1

        while page_number < max_pages and len(page.items) == page_size:
            if total < total_count_cap and slice_collected >= total:
                break
            if stats.requests >= MAX_REQUESTS:
                stats.truncated = True
                break
            page_number += 1
            payload["pageInfo"]["pageNumber"] = page_number
            page = client.query_products(payload)
            stats.requests += 1
            stats.pages += 1
            if not page.items:
                break
            slice_collected += consume(page.items)
            if len(page.items) < page_size:
                break

    items = _sort_items(list(collected.values()), sort)
    logger.info(
        "采集完成: %d 条, 页数=%d, 价格区间=%d, 截断=%s",
        len(items), stats.pages, stats.slices, stats.truncated,
    )
    return CollectResult(items=items, stats=stats)
