from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .errors import AmbiguousItemError, ItemNotFoundError
from .models import CatalogItem

_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")


def normalize_name(value: str) -> str:
    """与前端一致的名称归一化：NFC、去零宽、小写、空白折叠。"""
    if not value:
        return ""
    s = unicodedata.normalize("NFC", str(value))
    s = _ZERO_WIDTH.sub("", s)
    s = s.strip().lower()
    return re.sub(r"\s+", " ", s)


def compact_name(value: str) -> str:
    return normalize_name(value).replace(" ", "")


def resolve_item(
    catalog: Iterable[CatalogItem],
    *,
    name: str | None = None,
    item_id: str | None = None,
    item_type: str | None = None,
) -> CatalogItem:
    """把用户输入解析为唯一的 (ItemId, moduleType)。"""
    items = list(catalog)
    if item_type:
        item_type = item_type.strip().lower()
        if item_type == "transcend":
            item_type = "transcendent"

    if item_id:
        matched = [it for it in items if it.item_id == str(item_id).strip()]
        if item_type:
            matched = [it for it in matched if it.item_type == item_type]
        if not matched:
            raise ItemNotFoundError(f"ItemId {item_id!r} 不存在于 Jarvis 字典")
        if len(matched) > 1:
            raise AmbiguousItemError(
                f"ItemId {item_id!r} 对应多个条目: "
                + ", ".join(f"{m.names.get('cn', m.item_id)}({m.item_type})" for m in matched)
            )
        return matched[0]

    if not name:
        raise ItemNotFoundError("必须提供物品名称或 ItemId")

    query = normalize_name(name)
    query_compact = compact_name(name)
    pool = items
    if item_type:
        pool = [it for it in pool if it.item_type == item_type]

    def _match_exact(it: CatalogItem) -> bool:
        return any(
            normalize_name(v) == query or compact_name(v) == query_compact
            for v in it.names.values()
            if v
        )

    exact = [it for it in pool if _match_exact(it)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        types = {it.item_type for it in exact}
        if len(types) == 1:
            # 同一类型下重复条目，取排序靠前的一个
            return exact[0]
        raise AmbiguousItemError(
            f"名称 {name!r} 同时匹配多种类型: "
            + ", ".join(f"{it.names.get('cn', it.item_id)}({it.item_type}, id={it.item_id})" for it in exact)
            + "。请用 item.type 或 item.id 指定。"
        )

    fuzzy = [
        it for it in pool
        if any(query in normalize_name(v) or query_compact in compact_name(v)
               for v in it.names.values() if v)
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise AmbiguousItemError(
            f"名称 {name!r} 模糊匹配到多个物品: "
            + ", ".join(
                f"{it.names.get('cn', it.item_id)}({it.item_type}, id={it.item_id})"
                for it in fuzzy[:8]
            )
            + "。请提供更完整的名称。"
        )
    raise ItemNotFoundError(f"字典中没有找到 {name!r}")
