from __future__ import annotations

import logging
import os
import time
from typing import Any

from .errors import ApiError, HttpError
from .models import (
    CatalogItem,
    OptionEntry,
    OptionLists,
    Page,
    normalize_item_type,
)
from .transport import CurlTransport, Transport, parse_json

logger = logging.getLogger(__name__)

API_ROOT = "https://tfd-api.nexon.com/api/v2/trademarket"
JARVIS_URL = "https://jarvis-api.nexon.com/v1/contents/list/TFD_ETC_TradeMarket"
# Jarvis 内容接口的应用级令牌，取自官网前端公开代码（tfd.nexon.com 的 JS bundle）。
# 所有访客共用同一个值，不是个人凭据；Nexon 可能随时轮换，
# 可通过环境变量 TFD_JARVIS_TOKEN 覆盖。
DEFAULT_JARVIS_TOKEN = "F8BDFA4E-01E1-4D09-AB91-6BD111FE35F9"
JARVIS_TOKEN = os.getenv("TFD_JARVIS_TOKEN", "").strip() or DEFAULT_JARVIS_TOKEN
JARVIS_HEADERS = {
    "Authorization": f"Bearer {JARVIS_TOKEN}",
    "x-jarvis-api-key": "jarvis_tfd",
}

PRODUCTS_CONTENT_TYPE = "application/json-patch+json"


class MarketClient:
    """v2 市场只读客户端（匿名即可读取）。"""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        language: str = "cn",
        page_size: int = 20,
        min_interval: float = 0.2,
        max_retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._transport = transport or CurlTransport()
        self._owns_transport = transport is None
        self.language = language
        self.page_size = page_size
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._timeout = timeout
        self._last_request_at = 0.0
        self.request_count = 0

    # ── 基础设施 ──────────────────────────────────────────────

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _post(self, path: str, payload: dict[str, Any], content_type: str) -> dict[str, Any]:
        url = f"{API_ROOT}{path}"
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            self.request_count += 1
            try:
                resp = self._transport.request(
                    "POST",
                    url,
                    json_body=payload,
                    content_type=content_type,
                    timeout=self._timeout,
                )
            except Exception as exc:  # noqa: BLE001 — 网络层异常统一重试
                last_error = exc
                if attempt == self._max_retries:
                    raise HttpError(0, f"network error: {exc}") from exc
                time.sleep(2 ** attempt * 0.5)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = HttpError(resp.status_code, resp.text)
                if attempt == self._max_retries:
                    raise last_error
                time.sleep(2 ** attempt * 0.5)
                continue
            return parse_json(resp)
        raise last_error or HttpError(0, "unknown error")

    def _get(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            self.request_count += 1
            try:
                resp = self._transport.request("GET", url, headers=headers, timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == self._max_retries:
                    raise HttpError(0, f"network error: {exc}") from exc
                time.sleep(2 ** attempt * 0.5)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = HttpError(resp.status_code, resp.text)
                if attempt == self._max_retries:
                    raise last_error
                time.sleep(2 ** attempt * 0.5)
                continue
            return parse_json(resp)
        raise last_error or HttpError(0, "unknown error")

    @staticmethod
    def _check_success(data: dict[str, Any]) -> None:
        code = data.get("ResultCode")
        if code != "SUCCESS":
            raise ApiError(code or "UNKNOWN", str(data.get("ResultMessage") or ""))

    # ── 业务接口 ──────────────────────────────────────────────

    def fetch_catalog(self) -> list[CatalogItem]:
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                catalog = self._fetch_catalog_once()
            except (ApiError, HttpError) as exc:
                last_error = exc
                logger.warning("Jarvis 字典拉取失败（第 %s 次）: %s", attempt, exc)
                time.sleep(attempt)
                continue
            if catalog:
                logger.info("Jarvis catalog: %d items", len(catalog))
                return catalog
            logger.warning("Jarvis 字典返回空（第 %s 次）", attempt)
            time.sleep(attempt)
        raise ApiError(
            "EMPTY_CATALOG",
            f"Jarvis 物品字典为空或不可用: {last_error}" if last_error else "Jarvis 物品字典为空",
        )

    def _fetch_catalog_once(self) -> list[CatalogItem]:
        data = self._get(JARVIS_URL, headers=JARVIS_HEADERS)
        code = str(data.get("code") or "")
        if code and code != "0000":
            raise ApiError(code, str(data.get("message") or ""))
        entries = data.get("result") or data.get("ResultData") or []
        if not entries:
            logger.warning("Jarvis 返回空列表，原始响应: %s", str(data)[:400])
        catalog: list[CatalogItem] = []
        skipped = 0
        for entry in entries:
            # Jarvis 在不同边缘节点会返回 ItemId 或 itemId，统一按小写键读取
            e = {str(k).lower(): v for k, v in entry.items()}
            if e.get("isdeleted"):
                skipped += 1
                continue
            item_id = str(e.get("itemid") or "").strip()
            module_type = str(e.get("moduletype") or "").strip().lower()
            if not item_id or module_type not in ("ancestor", "trigger", "transcendent", "descendant"):
                skipped += 1
                continue
            names = {
                loc: str(e.get(loc) or "")
                for loc in ("cn", "tw", "en", "ko", "ja", "de", "fr", "es", "pt", "pl", "it")
                if e.get(loc)
            }
            catalog.append(
                CatalogItem(
                    item_id=item_id,
                    item_type=normalize_item_type(module_type),
                    names=names,
                    is_active=bool(e.get("isactive", True)),
                    is_default=bool(e.get("isdefault")),
                    is_blocked_default=bool(e.get("isblockeddefault")),
                )
            )
        if skipped:
            logger.warning("Jarvis 条目被跳过: %d / %d", skipped, len(entries))
        if entries and not catalog:
            logger.warning("Jarvis 全部条目不可用，样例: %s", str(entries[0])[:600])
        return catalog

    def fetch_filter(self, filter_type: str, sub_type: str = "", language: str | None = None) -> list[dict[str, Any]]:
        payload = {
            "now": None,
            "maintenance": None,
            "restrict": None,
            "access": None,
            "filterType": filter_type,
            "subType": sub_type,
        }
        lang = language or self.language
        data = self._post(f"/{lang}/filter", payload, "application/json")
        self._check_success(data)
        result = data.get("ResultData")
        return result if isinstance(result, list) else []

    @staticmethod
    def _merge_option(bucket: dict[str, OptionEntry], entry_id: str, value: str) -> None:
        if not entry_id:
            return
        if entry_id not in bucket:
            bucket[entry_id] = OptionEntry(id=entry_id, value=value)
            return
        current = bucket[entry_id]
        if value and value != current.value and value not in current.aliases:
            current.aliases.append(value)

    def fetch_option_lists(self, item_type: str, item_id: str) -> OptionLists:
        """拉取该物品可用的属性/辅助能力/品级列表。

        同时请求当前语言、中文、英文三套文案，按 ID 合并为「主名称 + 别名」，
        这样 --subperk / --advantage 既可以用中文名，也可以用英文名。
        """
        languages: list[str] = []
        for lang in (self.language, "cn", "en"):
            if lang and lang not in languages:
                languages.append(lang)

        advantage: dict[str, OptionEntry] = {}
        penalty: dict[str, OptionEntry] = {}
        subperks: dict[str, OptionEntry] = {}
        grades: dict[str, OptionEntry] = {}

        for index, lang in enumerate(languages):
            try:
                groups = self.fetch_filter("ITEMOPTION", f"{item_type}:{item_id}", language=lang)
                for group in groups:
                    gtype = str(group.get("type") or "").lower()
                    bucket = advantage if gtype == "advantage" else penalty if gtype == "penalty" else None
                    if bucket is None:
                        continue
                    for it in group.get("list") or []:
                        self._merge_option(bucket, str(it.get("id") or ""), str(it.get("value") or ""))

                groups = self.fetch_filter("SUBPERK", item_id, language=lang)
                for group in groups:
                    if str(group.get("type") or "").upper() == "SUBPERK":
                        for it in group.get("list") or []:
                            self._merge_option(subperks, str(it.get("id") or ""), str(it.get("value") or ""))
                        break

                groups = self.fetch_filter("GRADE", "", language=lang)
                for group in groups:
                    if str(group.get("type") or "").upper() == "GRADE":
                        for it in group.get("list") or []:
                            self._merge_option(grades, str(it.get("id") or ""), str(it.get("value") or ""))
                        break
            except (ApiError, HttpError) as exc:
                if index == 0:
                    raise
                logger.debug("附加语言 %s 的筛选项拉取失败（不影响主语言）: %s", lang, exc)

        return OptionLists(
            advantage=list(advantage.values()),
            penalty=list(penalty.values()),
            subperks=list(subperks.values()),
            grades=list(grades.values()),
        )

    def query_products(self, payload: dict[str, Any]) -> Page:
        data = self._post(f"/{self.language}/products", payload, PRODUCTS_CONTENT_TYPE)
        if data.get("ResultCode") == "NOT_MATCHED_ALLOWED":
            raise ApiError("NOT_MATCHED_ALLOWED", "strItemType 与 strItemid 不匹配")
        self._check_success(data)
        result = data.get("ResultData") or {}
        items = result.get("items")
        items = items if isinstance(items, list) else []
        total = result.get("totalCount")
        total = int(total) if isinstance(total, (int, float)) else None
        return Page(items=items, total_count=total)

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()
