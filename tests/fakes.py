from __future__ import annotations

import copy
from typing import Any, Callable

from market_v2.models import CatalogItem, OptionEntry, OptionLists, Page
from market_v2.transport import Response


class FakeTransport:
    """把 (method, url, json_body) 交给 handler，方便测试 API 解析层。"""

    def __init__(self, handler: Callable[[str, str, dict[str, Any] | None], Response]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> Response:
        self.calls.append((method, url, json_body))
        return self._handler(method, url, json_body)

    def close(self) -> None:
        return None


def catalog_item(
    item_id: str,
    item_type: str,
    cn: str,
    en: str | None = None,
) -> CatalogItem:
    names = {"cn": cn}
    if en:
        names["en"] = en
    return CatalogItem(item_id=item_id, item_type=item_type, names=names)


def make_raw(
    n8_id: str,
    price: float,
    *,
    item_name: str = "怪物猎人",
    item_type: str = "transcendent",
    dt: str = "2026-10-21T00:00:00.000",
    quality: int = 400,
    subperks: list[str] | None = None,
    groups: dict[int, int] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"n4WeaponQuality": quality}
    data["subperks"] = [{"strName": name, "isFixed": 0, "options": []} for name in (subperks or [])]
    groups = groups or {}
    data["options"] = [
        {"strDesc": f"group {g}", "n4Point": str(point), "n4Group": g}
        for g, point in sorted(groups.items())
    ]
    return {
        "n8Id": n8_id,
        "strItemName": item_name,
        "strType": item_type,
        "n4SalePrice": str(price),
        "dtRegister": dt,
        "seller": {"strNickName": "seller#0001", "isAccessState": 1, "n4MasteryRank": 30},
        "data": data,
    }


class FuncClient:
    """按 payload 返回 Page 的脚本化客户端。"""

    def __init__(self, responder: Callable[[dict[str, Any]], Page]) -> None:
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    def query_products(self, payload: dict[str, Any]) -> Page:
        self.calls.append(copy.deepcopy(payload))
        return self._responder(payload)


class FullFakeClient(FuncClient):
    """供 runner 端到端测试使用，补齐 catalog / option_lists。"""

    def __init__(
        self,
        catalog: list[CatalogItem],
        option_lists: OptionLists,
        responder: Callable[[dict[str, Any]], Page],
    ) -> None:
        super().__init__(responder)
        self._catalog = catalog
        self._option_lists = option_lists

    def fetch_catalog(self) -> list[CatalogItem]:
        return self._catalog

    def fetch_option_lists(self, item_type: str, item_id: str) -> OptionLists:
        return self._option_lists

    def close(self) -> None:
        return None


def transcendent_options() -> OptionLists:
    return OptionLists(
        subperks=[
            OptionEntry(id="20017:0", value="强化大容量弹夹"),
            OptionEntry(id="20000:1", value="强化弹头 I"),
            OptionEntry(id="20001:2", value="精密弹夹"),
        ],
        grades=[
            OptionEntry(id="Tier1", value="普通"),
            OptionEntry(id="Tier4", value="超越"),
        ],
    )


def ancestor_options() -> OptionLists:
    return OptionLists(
        advantage=[
            OptionEntry(id="301900006", value="技能冷却时间"),
            OptionEntry(id="301900007", value="技能资源消耗量"),
        ],
        penalty=[
            OptionEntry(id="301900034", value="技能冷却时间"),
            OptionEntry(id="301900035", value="技能资源消耗量"),
        ],
        grades=[
            OptionEntry(id="Tier1", value="普通"),
            OptionEntry(id="Tier4", value="超越"),
        ],
    )
