from __future__ import annotations

from market_v2.filters import FilterSpec, IntRange
from market_v2.models import Page
from market_v2.runner import run_spec
from tests.fakes import FullFakeClient, catalog_item, make_raw, transcendent_options


def test_run_spec_end_to_end_without_writing():
    catalog = [catalog_item("211064002", "transcendent", "怪物猎人")]
    raw_items = [
        make_raw("a", 1200, quality=400, subperks=["强化大容量弹夹"], groups={0: 120, 1: 30, 2: 20}),
        make_raw("b", 800, quality=380, subperks=["强化大容量弹夹"], groups={0: 110, 1: 20, 2: 10}),
    ]
    client = FullFakeClient(
        catalog,
        transcendent_options(),
        lambda payload: Page(items=raw_items, total_count=2),
    )
    spec = FilterSpec(
        item_name="怪物猎人",
        price=IntRange(100, 5000),
        quality=IntRange(1, 400),
        subperks=["强化大容量弹夹"],
        ability1=IntRange(100, 150),
    )

    outcome = run_spec(spec, client, write=False)

    assert outcome.item.item_id == "211064002"
    assert outcome.document["stats"]["matched"] == 2
    assert outcome.document["filters"]["subperks"][0]["id"] == "20017:0"
    assert outcome.payload["transcendent"]["abilityPoint1"] == {"n4ValueMin": 100, "n4ValueMax": 150}
    assert outcome.path is None


def test_run_spec_writes_json_file(tmp_path):
    catalog = [catalog_item("211064002", "transcendent", "怪物猎人")]
    client = FullFakeClient(
        catalog,
        transcendent_options(),
        lambda payload: Page(items=[make_raw("a", 100)], total_count=1),
    )
    spec = FilterSpec(item_name="怪物猎人", price=IntRange(10, 5000), output_dir=str(tmp_path))

    outcome = run_spec(spec, client, write=True)

    assert outcome.path is not None
    assert outcome.path.exists()
    assert outcome.path.parent == tmp_path
    assert outcome.path.name.endswith("_怪物猎人.json")
