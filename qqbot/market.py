"""市场数据服务：给机器人提供缓存、物品解析和「只取前几页」的抓取。

与 CLI 的区别：
- 物品字典缓存 24 小时，属性/辅助能力缓存 12 小时；
- 定时检查只抓前 N 页（默认 3 页），不做全量翻页/价格切分；
- 每个线程持有独立的 MarketClient（该类不是线程安全的）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from market_v2.api import MarketClient
from market_v2.catalog import normalize_name, resolve_item
from market_v2.errors import (
    AmbiguousItemError,
    FilterSpecError,
    ItemNotFoundError,
    MarketError,
)
from market_v2.filters import FilterSpec, build_payload
from market_v2.models import CatalogItem, OptionLists

from .dsl import ITEM_TYPE_LABELS

PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 3


class MarketServiceError(Exception):
    """可直接展示给用户的错误。"""


@dataclass
class FetchResult:
    item: CatalogItem
    resolved: dict[str, Any]
    items: list[dict[str, Any]]
    requests: int
    total_count: int | None = None
    complete: bool = True

    @property
    def ids(self) -> list[str]:
        return [str(it.get("n8Id") or "") for it in self.items if it.get("n8Id")]


class MarketService:
    def __init__(
        self,
        *,
        catalog_ttl: int = 86400,
        option_ttl: int = 43200,
        min_interval: float = 0.5,
        language: str = "cn",
    ) -> None:
        self.catalog_ttl = catalog_ttl
        self.option_ttl = option_ttl
        self.min_interval = min_interval
        self.language = language

        self._local = threading.local()
        self._clients: list[MarketClient] = []
        self._clients_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._catalog: list[CatalogItem] | None = None
        self._catalog_at = 0.0
        self._options: dict[tuple[str, str], tuple[OptionLists, float]] = {}

    # ── 客户端 ──────────────────────────────────────────────

    def _client(self) -> MarketClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = MarketClient(language=self.language, min_interval=self.min_interval)
            self._local.client = client
            with self._clients_lock:
                self._clients.append(client)
        return client

    def close(self) -> None:
        with self._clients_lock:
            for client in self._clients:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._clients.clear()

    # ── 缓存 ────────────────────────────────────────────────

    def catalog(self, *, force: bool = False) -> list[CatalogItem]:
        now = time.time()
        if not force:
            with self._cache_lock:
                if self._catalog is not None and now - self._catalog_at < self.catalog_ttl:
                    return self._catalog
        with self._catalog_lock:
            if not force:
                with self._cache_lock:
                    if self._catalog is not None and now - self._catalog_at < self.catalog_ttl:
                        return self._catalog
            catalog = self._client().fetch_catalog()
            with self._cache_lock:
                self._catalog = catalog
                self._catalog_at = time.time()
            return catalog

    def options(self, item: CatalogItem) -> OptionLists:
        key = (item.item_type, item.item_id)
        now = time.time()
        with self._cache_lock:
            cached = self._options.get(key)
            if cached and now - cached[1] < self.option_ttl:
                return cached[0]
        lists = self._client().fetch_option_lists(item.item_type, item.item_id)
        with self._cache_lock:
            self._options[key] = (lists, time.time())
        return lists

    # ── 物品解析 / 搜索 ─────────────────────────────────────

    def resolve(self, query: str, item_type: str | None = None) -> CatalogItem:
        try:
            catalog = self.catalog()
            item_id = query if query.isdigit() else None
            return resolve_item(
                catalog,
                name=query,
                item_id=item_id,
                item_type=item_type,
            )
        except (ItemNotFoundError, AmbiguousItemError) as exc:
            raise MarketServiceError(str(exc)) from exc
        except MarketError as exc:
            raise MarketServiceError(f"市场接口错误：{exc}") from exc

    def search_items(self, query: str, limit: int = 10) -> list[CatalogItem]:
        q = normalize_name(query)
        if not q:
            return []
        catalog = self.catalog()
        scored: list[tuple[int, CatalogItem]] = []
        for item in catalog:
            names = [normalize_name(n) for n in item.names.values() if n]
            if any(n == q for n in names):
                scored.append((0, item))
            elif any(q in n for n in names):
                scored.append((1, item))
        scored.sort(key=lambda x: (x[0], ITEM_TYPE_LABELS.get(x[1].item_type, ""), x[1].item_id))
        return [item for _, item in scored[:limit]]

    # ── 抓取 ────────────────────────────────────────────────

    def fetch(self, spec: FilterSpec, *, max_pages: int = DEFAULT_MAX_PAGES) -> FetchResult:
        try:
            item = self.resolve(
                spec.item_name or spec.item_id or "",
                spec.item_type,
            )
            spec.validate_for_type(item.item_type)
            options = self.options(item)
            payload, resolved = build_payload(spec, item, options)

            items: dict[str, dict[str, Any]] = {}
            requests = 0
            total_count: int | None = None
            complete = False
            for page_number in range(1, max_pages + 1):
                payload["pageInfo"] = {"pageNumber": page_number, "pageSize": PAGE_SIZE}
                page = self._client().query_products(payload)
                requests += 1
                if page.total_count is not None:
                    total_count = int(page.total_count)
                for raw in page.items:
                    key = str(raw.get("n8Id") or "")
                    if key and key not in items:
                        items[key] = raw
                if len(page.items) < PAGE_SIZE:
                    complete = True
                    break
                if total_count is not None and len(items) >= total_count:
                    complete = True
                    break
            return FetchResult(
                item=item,
                resolved=resolved,
                items=list(items.values()),
                requests=requests,
                total_count=total_count,
                complete=complete,
            )
        except MarketServiceError:
            raise
        except (FilterSpecError, MarketError) as exc:
            raise MarketServiceError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — 网络/解析异常统一转成可读错误
            raise MarketServiceError(f"抓取失败：{exc}") from exc

    @staticmethod
    def type_label(item: CatalogItem) -> str:
        return ITEM_TYPE_LABELS.get(item.item_type, item.item_type)
