from __future__ import annotations

import pytest

from market_v2.catalog import compact_name, normalize_name, resolve_item
from market_v2.errors import AmbiguousItemError, ItemNotFoundError
from tests.fakes import catalog_item

CATALOG = [
    catalog_item("211064002", "transcendent", "怪物猎人", "Monster Hunter"),
    catalog_item("261001023", "ancestor", "阴影的决意", "Shadow's Resolve"),
    catalog_item("255000045", "trigger", "暗中重整", "Prepping in the Dark"),
    catalog_item("101000036", "descendant", "终极海莉", "Ultimate Hailey"),
    catalog_item("261001017", "ancestor", "喜悦决意", "Joyous Resolve"),
    catalog_item("255000099", "trigger", "喜悦决意", "Joyous Resolve"),
    catalog_item("261001016", "ancestor", "神枪手决意", "Sniper's Resolve"),
]


def test_normalize_name_handles_spaces_and_width():
    assert normalize_name("  统治者   决意 ") == "统治者 决意"
    assert compact_name("统治者 决意") == compact_name("统治者决意")


def test_resolve_exact_chinese_name():
    item = resolve_item(CATALOG, name="怪物猎人")
    assert item.item_id == "211064002"
    assert item.item_type == "transcendent"


def test_resolve_by_english_name():
    item = resolve_item(CATALOG, name="monster hunter")
    assert item.item_id == "211064002"


def test_resolve_fuzzy_single_hit():
    item = resolve_item(CATALOG, name="海莉")
    assert item.item_id == "101000036"


def test_resolve_ambiguous_name_requires_type():
    with pytest.raises(AmbiguousItemError) as exc:
        resolve_item(CATALOG, name="喜悦决意")
    assert "ancestor" in str(exc.value)

    item = resolve_item(CATALOG, name="喜悦决意", item_type="trigger")
    assert item.item_id == "255000099"


def test_resolve_by_item_id():
    item = resolve_item(CATALOG, item_id="261001023")
    assert item.names["cn"] == "阴影的决意"


def test_resolve_by_item_id_with_wrong_type_raises():
    with pytest.raises(ItemNotFoundError):
        resolve_item(CATALOG, item_id="261001023", item_type="transcendent")


def test_resolve_unknown_name():
    with pytest.raises(ItemNotFoundError):
        resolve_item(CATALOG, name="不存在的物品")
