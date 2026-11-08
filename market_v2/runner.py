from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import MarketClient
from .catalog import resolve_item
from .collector import collect
from .exporter import build_document, write_document
from .filters import FilterSpec, build_payload
from .models import CatalogItem, CollectResult, OptionLists


@dataclass
class RunResult:
    item: CatalogItem
    spec: FilterSpec
    payload: dict[str, Any]
    resolved_filters: dict[str, Any]
    result: CollectResult
    document: dict[str, Any]
    path: Any = None


def run_spec(
    spec: FilterSpec,
    client: MarketClient | None = None,
    *,
    write: bool = True,
    catalog: list[CatalogItem] | None = None,
    option_lists: OptionLists | None = None,
) -> RunResult:
    owns_client = client is None
    client = client or MarketClient(language=spec.language)
    client.language = spec.language
    try:
        catalog = catalog or client.fetch_catalog()
        item = resolve_item(
            catalog,
            name=spec.item_name,
            item_id=spec.item_id,
            item_type=spec.item_type,
        )
        spec.validate()
        spec.validate_for_type(item.item_type)

        if option_lists is None:
            option_lists = client.fetch_option_lists(item.item_type, item.item_id)

        payload, resolved = build_payload(spec, item, option_lists)
        result = collect(
            client,
            payload,
            price_min=spec.price.min,
            price_max=spec.price.max,
            sort=spec.sort,
        )
        document = build_document(
            item=item,
            locale=spec.language,
            resolved_filters=resolved,
            payload=payload,
            items=result.items,
            stats=result.stats,
        )
        path = write_document(document, spec.output_dir, item.name(spec.language)) if write else None
        return RunResult(
            item=item,
            spec=spec,
            payload=payload,
            resolved_filters=resolved,
            result=result,
            document=document,
            path=path,
        )
    finally:
        if owns_client:
            client.close()
