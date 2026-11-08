from __future__ import annotations

import json
from datetime import datetime

from market_v2.exporter import build_document, build_filename, normalize_product, safe_filename, write_document
from market_v2.models import CollectStats
from tests.fakes import catalog_item, make_raw


def test_filename_is_date_plus_item_name():
    ts = datetime.fromisoformat("2026-10-21T09:30:00+08:00")
    assert build_filename("怪物猎人", ts) == "2026-10-21_怪物猎人.json"


def test_filename_sanitizes_illegal_characters():
    assert safe_filename('a/b\\c:*?"<>|') == "a_b_c_______"
    assert build_filename("怪物/猎人", datetime.fromisoformat("2026-10-21T00:00:00+08:00")) == "2026-10-21_怪物_猎人.json"


def test_normalize_product_converts_price_and_timezone():
    raw = make_raw("898484701286067202", 1200, dt="2026-10-21T00:00:00.000", quality=400)
    item = normalize_product(raw)
    assert item["n8Id"] == "898484701286067202"
    assert item["price"] == 1200.0
    assert item["item_type"] == "transcendent"
    assert item["register_time_cst"] == "2026-10-21T08:00:00+08:00"
    assert item["data"]["n4WeaponQuality"] == 400


def test_document_shape_and_write(tmp_path):
    item = catalog_item("211064002", "transcendent", "怪物猎人")
    stats = CollectStats(pages=2, slices=1, requests=2, server_totals=[3])
    doc = build_document(
        item=item,
        locale="cn",
        resolved_filters={"price": {"min": 100, "max": 5000}},
        payload={"strItemType": "transcendent", "strItemid": "211064002"},
        items=[make_raw("a", 100), make_raw("b", 200)],
        stats=stats,
    )
    assert doc["item"]["name"] == "怪物猎人"
    assert doc["stats"]["matched"] == 2
    assert doc["generated_date"] == doc["generated_at"][:10]
    assert len(doc["products"]) == 2

    path = write_document(doc, tmp_path, "怪物猎人")
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["products"][0]["price"] == 100.0
    assert path.name.endswith("_怪物猎人.json")
    assert not path.with_suffix(".json.tmp").exists()


def test_write_document_does_not_overwrite_same_day(tmp_path):
    item = catalog_item("211064002", "transcendent", "怪物猎人")
    doc = build_document(
        item=item,
        locale="cn",
        resolved_filters={},
        payload={},
        items=[],
        stats=CollectStats(),
    )
    doc["generated_at"] = "2026-10-21T09:30:00+08:00"
    doc["generated_date"] = "2026-10-21"

    first = write_document(doc, tmp_path, "怪物猎人")
    second = write_document(doc, tmp_path, "怪物猎人")

    assert first.name == "2026-10-21_怪物猎人.json"
    assert second.name.startswith("2026-10-21_怪物猎人_")
    assert first.exists() and second.exists()
