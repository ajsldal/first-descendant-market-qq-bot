from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ITEM_TYPES = ("ancestor", "trigger", "transcendent", "descendant")

# 服务端排序参数
SORT_LATEST = 1
SORT_PRICE_DESC = 2
SORT_PRICE_ASC = 3

SORT_ALIASES = {
    "latest": SORT_LATEST,
    "date": SORT_LATEST,
    "price_desc": SORT_PRICE_DESC,
    "highest": SORT_PRICE_DESC,
    "price_asc": SORT_PRICE_ASC,
    "lowest": SORT_PRICE_ASC,
}

# 服务端限制（实测）
MIN_PRICE = 10
MAX_PRICE = 25000
MIN_QUALITY = 1
MAX_QUALITY = 400
MIN_ABILITY_POINT = 0
MAX_ABILITY_POINT = 150
PAGE_SIZE = 20
MAX_PAGES = 500
TOTAL_COUNT_CAP = 10000


def normalize_item_type(value: str) -> str:
    v = (value or "").strip().lower()
    if v == "transcend":
        v = "transcendent"
    if v not in ITEM_TYPES:
        raise ValueError(f"unsupported item type: {value!r}")
    return v


@dataclass(frozen=True)
class CatalogItem:
    item_id: str
    item_type: str
    names: dict[str, str]
    is_active: bool = True
    is_default: bool = False
    is_blocked_default: bool = False

    def name(self, locale: str = "cn") -> str:
        return (
            self.names.get(locale)
            or self.names.get("cn")
            or self.names.get("en")
            or next(iter(self.names.values()), self.item_id)
        )


@dataclass
class Page:
    items: list[dict[str, Any]] = field(default_factory=list)
    total_count: int | None = None


@dataclass
class OptionEntry:
    id: str
    value: str
    aliases: list[str] = field(default_factory=list)

    def all_names(self) -> list[str]:
        names = [self.value] if self.value else []
        for alias in self.aliases:
            if alias and alias not in names:
                names.append(alias)
        return names


@dataclass
class OptionLists:
    advantage: list[OptionEntry] = field(default_factory=list)
    penalty: list[OptionEntry] = field(default_factory=list)
    subperks: list[OptionEntry] = field(default_factory=list)
    grades: list[OptionEntry] = field(default_factory=list)


@dataclass
class CollectStats:
    pages: int = 0
    slices: int = 0
    requests: int = 0
    truncated: bool = False
    server_totals: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        totals = [t for t in self.server_totals if t is not None]
        return {
            "pages_fetched": self.pages,
            "price_slices": self.slices,
            "requests": self.requests,
            "truncated": self.truncated,
            "server_total_max": max(totals) if totals else 0,
            "server_totals": self.server_totals,
        }


@dataclass
class CollectResult:
    items: list[dict[str, Any]]
    stats: CollectStats
