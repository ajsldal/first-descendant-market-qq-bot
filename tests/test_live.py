from __future__ import annotations

import pytest

from market_v2.filters import FilterSpec, IntRange
from market_v2.runner import run_spec


@pytest.mark.live
def test_live_transcendent_high_price_slice():
    """真实 API：只取 25000 价位的超越武器，验证服务端筛选生效。"""
    spec = FilterSpec(
        item_name="怪物猎人",
        item_type="transcendent",
        price=IntRange(25000, 25000),
    )
    outcome = run_spec(spec, write=False)
    products = outcome.document["products"]

    assert products, "该价位应当存在挂单"
    assert all(p["price"] == 25000 for p in products)
    assert all(p["item_type"] == "transcendent" for p in products)


@pytest.mark.live
def test_live_ancestor_grade_filter():
    """真实 API：品级 Tier4 过滤后，每张卡至少有一条 Tier4 属性。"""
    spec = FilterSpec(
        item_name="阴影的决意",
        item_type="ancestor",
        grade="Tier4",
        price=IntRange(10, 200),
    )
    outcome = run_spec(spec, write=False)
    products = outcome.document["products"]

    assert products, "该条件下应当存在挂单"
    for p in products:
        options = (p["data"] or {}).get("options") or []
        assert any(o.get("strTier") == "Tier4" for o in options)
