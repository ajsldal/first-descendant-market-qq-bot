from __future__ import annotations

import json

from market_v2.api import MarketClient
from market_v2.transport import Response
from tests.fakes import FakeTransport


def _catalog_response(entries):
    return Response(status_code=200, text=json.dumps({"code": "0000", "message": "SUCCESS", "result": entries}))


def test_catalog_parses_pascal_case_item_id():
    entries = [
        {"ItemId": "211064002", "moduleType": "transcendent", "cn": "怪物猎人", "isActive": True},
        {"ItemId": "261001023", "moduleType": "ancestor", "cn": "阴影的决意", "isActive": True},
    ]
    client = MarketClient(transport=FakeTransport(lambda m, u, b: _catalog_response(entries)))
    catalog = client.fetch_catalog()
    assert [c.item_id for c in catalog] == ["211064002", "261001023"]


def test_catalog_parses_camel_case_item_id():
    """Jarvis 部分边缘节点返回 itemId（小写 d），必须兼容。"""
    entries = [
        {"itemId": "211064002", "moduleType": "transcendent", "cn": "怪物猎人", "isActive": True},
        {"itemId": "255000045", "moduleType": "trigger", "cn": "暗中重整", "isActive": True},
    ]
    client = MarketClient(transport=FakeTransport(lambda m, u, b: _catalog_response(entries)))
    catalog = client.fetch_catalog()
    assert [c.item_id for c in catalog] == ["211064002", "255000045"]
    assert catalog[0].name("cn") == "怪物猎人"


def test_catalog_skips_deleted_entries():
    entries = [
        {"ItemId": "1", "moduleType": "ancestor", "cn": "A", "isDeleted": True},
        {"ItemId": "2", "moduleType": "ancestor", "cn": "B"},
    ]
    client = MarketClient(transport=FakeTransport(lambda m, u, b: _catalog_response(entries)))
    catalog = client.fetch_catalog()
    assert [c.item_id for c in catalog] == ["2"]


def test_products_raises_on_error_result_code():
    from market_v2.errors import ApiError
    import pytest

    body = Response(
        status_code=200,
        text=json.dumps({"IsSuccess": False, "ResultCode": "INVALID_PARAM", "ResultMessage": "strItemid:nullable"}),
    )
    client = MarketClient(transport=FakeTransport(lambda m, u, b: body))
    with pytest.raises(ApiError) as exc:
        client.query_products({"pageInfo": {"pageNumber": 1, "pageSize": 20}})
    assert exc.value.result_code == "INVALID_PARAM"


def test_products_parses_items_and_total():
    body = Response(
        status_code=200,
        text=json.dumps(
            {
                "IsSuccess": True,
                "ResultCode": "SUCCESS",
                "ResultData": {"items": [{"n8Id": "1"}], "totalCount": 42},
            }
        ),
    )
    client = MarketClient(transport=FakeTransport(lambda m, u, b: body))
    page = client.query_products({"pageInfo": {"pageNumber": 1, "pageSize": 20}})
    assert page.total_count == 42
    assert page.items == [{"n8Id": "1"}]


CN_FILTERS = {
    "ITEMOPTION": [
        {"type": "Advantage", "list": [{"id": "", "value": "全部"}, {"id": "301900006", "value": "技能冷却时间"}]},
        {"type": "Penalty", "list": [{"id": "", "value": "全部"}, {"id": "301900034", "value": "技能冷却时间"}]},
    ],
    "SUBPERK": [
        {"type": "SUBPERK", "list": [{"id": "", "value": "全部"}, {"id": "20136:0", "value": "多重打击转换"}]}
    ],
    "GRADE": [{"type": "GRADE", "list": [{"id": "Tier1", "value": "普通"}]}],
}

EN_FILTERS = {
    "ITEMOPTION": [
        {"type": "Advantage", "list": [{"id": "", "value": "All"}, {"id": "301900006", "value": "Cooldown"}]},
        {"type": "Penalty", "list": [{"id": "", "value": "All"}, {"id": "301900034", "value": "Cooldown"}]},
    ],
    "SUBPERK": [
        {"type": "SUBPERK", "list": [{"id": "", "value": "All"}, {"id": "20136:0", "value": "Multi-Hit Conversion"}]}
    ],
    "GRADE": [{"type": "GRADE", "list": [{"id": "Tier1", "value": "Common"}]}],
}


def _filter_handler(method, url, body):
    lang = url.split("/api/v2/trademarket/")[1].split("/")[0]
    filter_type = (body or {}).get("filterType")
    data = (CN_FILTERS if lang == "cn" else EN_FILTERS)[filter_type]
    return Response(
        status_code=200,
        text=json.dumps({"IsSuccess": True, "ResultCode": "SUCCESS", "ResultData": data}),
    )


def test_option_lists_merge_chinese_and_english_aliases():
    from market_v2.filters import resolve_entry

    client = MarketClient(transport=FakeTransport(_filter_handler), language="cn", min_interval=0)
    lists = client.fetch_option_lists("transcendent", "211064002")

    subperk = next(e for e in lists.subperks if e.id == "20136:0")
    assert "多重打击转换" in subperk.all_names()
    assert "Multi-Hit Conversion" in subperk.all_names()

    advantage = next(e for e in lists.advantage if e.id == "301900006")
    assert set(advantage.all_names()) == {"技能冷却时间", "Cooldown"}

    # 中文站点下也能用英文名解析
    assert resolve_entry(lists.subperks, "Multi-Hit Conversion", "辅助能力").id == "20136:0"


def test_option_lists_english_site_still_accepts_chinese():
    from market_v2.filters import resolve_entry

    client = MarketClient(transport=FakeTransport(_filter_handler), language="en", min_interval=0)
    lists = client.fetch_option_lists("transcendent", "211064002")

    subperk = next(e for e in lists.subperks if e.id == "20136:0")
    assert subperk.value == "Multi-Hit Conversion"
    assert "多重打击转换" in subperk.aliases
    assert resolve_entry(lists.subperks, "多重打击转换", "辅助能力").id == "20136:0"
