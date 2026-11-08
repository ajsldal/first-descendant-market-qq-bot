from __future__ import annotations

import pytest

from market_v2.errors import FilterSpecError, OptionResolveError
from market_v2.filters import FilterSpec, IntRange, OptionCondition, build_payload, resolve_entry
from market_v2.models import CatalogItem, OptionEntry
from tests.fakes import ancestor_options, catalog_item, transcendent_options

MONSTER = catalog_item("211064002", "transcendent", "怪物猎人")
SHADOW = catalog_item("261001023", "ancestor", "阴影的决意")
TRIGGER = catalog_item("255000045", "trigger", "暗中重整")


def test_transcendent_payload_matches_v2_contract():
    spec = FilterSpec(
        item_name="怪物猎人",
        price=IntRange(100, 5000),
        quality=IntRange(1, 400),
        subperks=["强化大容量弹夹", "精密弹夹"],
        ability1=IntRange(100, 150),
        ability2=IntRange(0, 50),
        ability3=IntRange(None, 150),
        sort="price_asc",
    )
    payload, resolved = build_payload(spec, MONSTER, transcendent_options())

    assert payload["strItemType"] == "transcendent"
    assert payload["strItemid"] == "211064002"
    assert payload["strGrade"] is None
    assert payload["n4SalePriceMin"] == 100
    assert payload["n4SalePriceMax"] == 5000
    assert payload["odType"] == 3
    assert "ancestor" not in payload
    assert payload["transcendent"] == {
        "quality": {"n4ValueMin": 1, "n4ValueMax": 400},
        "subperks": [{"strId": "20017:0"}, {"strId": "20001:2"}],
        "abilityPoint1": {"n4ValueMin": 100, "n4ValueMax": 150},
        "abilityPoint2": {"n4ValueMin": 0, "n4ValueMax": 50},
        "abilityPoint3": {"n4ValueMin": None, "n4ValueMax": 150},
    }
    assert resolved["abilities"] == [
        {"group": 1, "min": 100, "max": 150},
        {"group": 2, "min": 0, "max": 50},
        {"group": 3, "min": None, "max": 150},
    ]


def test_ancestor_payload_builds_options_and_grade():
    spec = FilterSpec(
        item_name="阴影的决意",
        price=IntRange(10, 2000),
        grade="Tier4",
        advantage=[OptionCondition("技能冷却时间", "Tier4")],
        penalty=OptionCondition("技能冷却时间"),
    )
    payload, resolved = build_payload(spec, SHADOW, ancestor_options())

    assert payload["strGrade"] == "TIER4"
    assert payload["ancestor"] == {
        "options": [
            {"strId": "301900006", "strGrade": "Tier4", "strSlot": "ADVANTAGE"},
            {"strId": "301900034", "strGrade": "NONE", "strSlot": "PENALTY"},
        ]
    }
    assert resolved["penalty"] == {"id": "301900034", "name": "技能冷却时间"}


def test_trigger_payload_has_no_ancestor_block():
    spec = FilterSpec(item_name="暗中重整", grade="Tier3")
    payload, _ = build_payload(spec, TRIGGER, ancestor_options())
    assert "ancestor" not in payload
    assert "transcendent" not in payload
    assert payload["strGrade"] == "TIER3"


def test_option_can_be_resolved_by_raw_id():
    spec = FilterSpec(item_name="阴影的决意", advantage=[OptionCondition("301900007")])
    payload, _ = build_payload(spec, SHADOW, ancestor_options())
    assert payload["ancestor"]["options"][0]["strId"] == "301900007"


def test_unknown_option_raises_with_candidates():
    with pytest.raises(OptionResolveError):
        resolve_entry([OptionEntry("1", "技能冷却时间")], "不存在的属性", "增益属性")


def test_option_resolves_by_cross_language_alias():
    entry = OptionEntry("20136:0", "Multi-Hit Conversion", ["多重打击转换"])
    assert resolve_entry([entry], "多重打击转换", "辅助能力").id == "20136:0"
    assert resolve_entry([entry], "multi-hit conversion", "辅助能力").id == "20136:0"
    assert resolve_entry([entry], "20136:0", "辅助能力").id == "20136:0"


def test_quality_on_ancestor_is_rejected():
    spec = FilterSpec(item_name="阴影的决意", quality=IntRange(1, 400))
    with pytest.raises(FilterSpecError):
        build_payload(spec, SHADOW, ancestor_options())


def test_advantage_on_transcendent_is_rejected():
    spec = FilterSpec(item_name="怪物猎人", advantage=[OptionCondition("技能冷却时间")])
    with pytest.raises(FilterSpecError):
        build_payload(spec, MONSTER, transcendent_options())


def test_more_than_two_subperks_is_rejected():
    spec = FilterSpec(item_name="怪物猎人", subperks=["a", "b", "c"])
    with pytest.raises(FilterSpecError):
        spec.validate()


def test_price_out_of_range_is_rejected():
    spec = FilterSpec(item_name="怪物猎人", price=IntRange(1, 5000))
    with pytest.raises(FilterSpecError):
        spec.validate()


def test_price_min_greater_than_max_is_rejected():
    spec = FilterSpec(item_name="怪物猎人", price=IntRange(5000, 100))
    with pytest.raises(FilterSpecError):
        spec.validate()


def test_grade_on_transcendent_is_rejected():
    spec = FilterSpec(item_name="怪物猎人", grade="Tier4")
    with pytest.raises(FilterSpecError):
        build_payload(spec, MONSTER, transcendent_options())


def test_from_dict_parses_string_forms():
    spec = FilterSpec.from_dict(
        {
            "item": {"name": "怪物猎人", "type": "transcendent"},
            "price": {"min": 100, "max": 5000},
            "quality": {"min": 1, "max": 400},
            "advantage": ["技能冷却时间:Tier4"],
            "ability1": "100-150",
            "sort": "latest",
        }
    )
    assert spec.item_type == "transcendent"
    assert spec.price == IntRange(100, 5000)
    assert spec.quality == IntRange(1, 400)
    assert spec.advantage[0].key == "技能冷却时间"
    assert spec.advantage[0].grade == "Tier4"
    assert spec.ability1 == IntRange(100, 150)


def test_grade_normalization_accepts_digits_and_upper():
    assert FilterSpec.from_dict({"item": "x", "grade": "4"}).grade == "Tier4"
    assert FilterSpec.from_dict({"item": "x", "grade": "TIER2"}).grade == "Tier2"
    assert FilterSpec.from_dict({"item": "x", "grade": ""}).grade is None
