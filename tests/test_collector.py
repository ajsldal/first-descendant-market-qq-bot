from __future__ import annotations

from market_v2.collector import collect
from market_v2.models import Page
from tests.fakes import FuncClient, make_raw

BASE = {
    "now": None,
    "maintenance": None,
    "restrict": None,
    "access": None,
    "pageInfo": {"pageNumber": 1, "pageSize": 20},
    "strLanguage": "cn",
    "strItemType": "transcendent",
    "strItemid": "211064002",
    "strGrade": None,
    "n4SalePriceMin": None,
    "n4SalePriceMax": None,
    "odType": 1,
}


def test_pagination_collects_all_pages_without_duplicates():
    pages = {
        1: [make_raw(f"id-{i}", 100 + i, dt=f"2026-10-21T00:00:{i:02d}.000") for i in range(20)],
        2: [make_raw(f"id-{i}", 100 + i, dt=f"2026-10-21T00:01:{i:02d}.000") for i in range(20, 40)],
        3: [make_raw(f"id-{i}", 100 + i, dt=f"2026-10-21T00:02:{i:02d}.000") for i in range(40, 45)],
    }

    def responder(payload):
        return Page(items=pages.get(payload["pageInfo"]["pageNumber"], []), total_count=45)

    client = FuncClient(responder)
    result = collect(client, BASE, price_min=10, price_max=1000)

    assert len(result.items) == 45
    assert result.stats.pages == 3
    assert result.stats.slices == 1
    assert result.stats.truncated is False
    assert len({it["n8Id"] for it in result.items}) == 45
    assert client.calls[0]["pageInfo"]["pageNumber"] == 1
    assert client.calls[1]["pageInfo"]["pageNumber"] == 2
    assert client.calls[2]["pageInfo"]["pageNumber"] == 3


def test_empty_result_is_handled():
    client = FuncClient(lambda payload: Page(items=[], total_count=None))
    result = collect(client, BASE, price_min=10, price_max=100)
    assert result.items == []
    assert result.stats.pages == 1
    assert result.stats.slices == 0


def test_total_count_cap_triggers_price_splitting():
    """区间命中封顶时自动切分价格，且结果不重复。"""
    cap = 100

    def responder(payload):
        lo = payload["n4SalePriceMin"]
        hi = payload["n4SalePriceMax"]
        page = payload["pageInfo"]["pageNumber"]
        width = hi - lo
        if width >= 100:
            items = [make_raw(f"{lo}-{hi}-{i}", lo + i) for i in range(20)]
            return Page(items=items, total_count=cap)
        items = [make_raw(f"leaf-{lo}-{hi}-{i}", lo + i) for i in range(3)]
        return Page(items=items if page == 1 else [], total_count=3)

    client = FuncClient(responder)
    result = collect(client, BASE, price_min=10, price_max=1000, total_count_cap=cap)

    assert result.stats.truncated is False
    assert result.stats.slices >= 2
    assert len(result.items) == len({it["n8Id"] for it in result.items})
    # 叶子区间进入分页抓取（page=1 且 total 小于封顶）
    leaf_first_pages = [
        c for c in client.calls
        if c["pageInfo"]["pageNumber"] == 1 and (c["n4SalePriceMax"] - c["n4SalePriceMin"]) < 100
    ]
    assert leaf_first_pages, "应当存在被切分后的叶子区间请求"


def test_single_price_point_at_cap_marks_truncated():
    def responder(payload):
        page = payload["pageInfo"]["pageNumber"]
        items = [make_raw(f"p{page}-{i}", 50) for i in range(20)]
        return Page(items=items, total_count=10000)

    client = FuncClient(responder)
    result = collect(client, BASE, price_min=50, price_max=50, max_pages=3, total_count_cap=100)
    assert result.stats.truncated is True
    assert result.stats.pages == 3


def test_max_pages_limit():
    def responder(payload):
        page = payload["pageInfo"]["pageNumber"]
        items = [make_raw(f"p{page}-{i}", 100) for i in range(20)]
        return Page(items=items, total_count=1000)

    client = FuncClient(responder)
    result = collect(client, BASE, price_min=10, price_max=100, max_pages=3)
    assert result.stats.pages == 3
    assert len(result.items) == 60


def test_sort_price_asc():
    def responder(payload):
        items = [make_raw("c", 300), make_raw("a", 100), make_raw("b", 200)]
        return Page(items=items, total_count=3)

    client = FuncClient(responder)
    result = collect(client, BASE, price_min=10, price_max=1000, sort="price_asc")
    assert [it["n4SalePrice"] for it in result.items] == ["100", "200", "300"]
