"""qqbot 核心逻辑测试：DSL、存储、调度、推送格式、向导。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from market_v2.models import CatalogItem
from qqbot.dsl import (
    DslError,
    describe_spec,
    fingerprint_for,
    parse_dsl,
    parse_settings,
)
from qqbot.store import Store, Subscription
from qqbot.market import FetchResult
from qqbot.pusher import format_result
from qqbot.scheduler import Scheduler, quiet_until
from qqbot.wizard import Wizard

ITEM = CatalogItem(
    item_id="211064002",
    item_type="transcendent",
    names={"cn": "怪物猎人", "en": "Monster Hunter"},
)


def _raw(n8: str, price: float, quality: int = 400) -> dict:
    return {
        "n8Id": n8,
        "n4SalePrice": price,
        "dtRegister": "2026-10-20T18:31:14.017",
        "seller": {"strNickName": "tester#1234"},
        "data": {"n4WeaponQuality": quality, "subperks": [{"strName": "强化弹夹"}]},
    }


class FakeMarket:
    def __init__(self, items=None):
        self.items = items or [_raw("1", 600), _raw("2", 700)]
        self.calls = 0

    def fetch(self, spec, max_pages=3):
        self.calls += 1
        return FetchResult(item=ITEM, resolved={}, items=self.items, requests=1)


class FakePusher:
    def __init__(self):
        self.calls = []

    async def push(self, sub, result, new_ids, *, items=None, same_as_last=False):
        self.calls.append(
            {
                "sub": sub.id,
                "items": items if items is not None else result.items,
                "new_ids": set(new_ids),
                "same": same_as_last,
            }
        )
        return len(items if items is not None else result.items)

    async def send_private(self, qq, content):
        self.calls.append({"notice": content})


def _store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


def _add(store: Store, qq=10001, fingerprint="fp1", *, only_new=False, first_push=True) -> int:
    return store.add_subscription(
        qq,
        {
            "item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"},
            "price": {"min": 100, "max": 5000},
        },
        fingerprint,
        interval_sec=600,
        max_push=20,
        only_new=only_new,
        quiet_hours="",
        first_push=first_push,
        next_run_at=0,
    )


# ── DSL ────────────────────────────────────────────────────


def test_parse_transcendent_dsl():
    parsed = parse_dsl("怪物猎人 价格=100-5000 品质=390-400 辅助=弹夹,弹头 能力1=30-150 间隔=10m")
    spec = parsed.spec
    assert spec.item_name == "怪物猎人"
    assert spec.price.min == 100 and spec.price.max == 5000
    assert spec.quality.min == 390 and spec.quality.max == 400
    assert spec.subperks == ["弹夹", "弹头"]
    assert spec.ability1.min == 30 and spec.ability1.max == 150
    assert parsed.options.interval_sec == 600
    assert parsed.options.only_new is False


def test_parse_module_dsl_with_grade():
    parsed = parse_dsl("阴影的决意 品级=4 增益=技能冷却时间:Tier4,技能资源消耗量 减益=最大护盾")
    spec = parsed.spec
    assert spec.grade == "Tier4"
    assert spec.advantage[0].key == "技能冷却时间"
    assert spec.advantage[0].grade == "Tier4"
    assert spec.penalty.key == "最大护盾"


def test_parse_item_name_with_spaces():
    parsed = parse_dsl("测试 武器 价格=100")
    assert parsed.spec.item_name == "测试 武器"


def test_parse_ranges_variants():
    assert parse_dsl("怪物猎人 价格=>=100").spec.price.min == 100
    assert parse_dsl("怪物猎人 价格=<=500").spec.price.max == 500
    assert parse_dsl("怪物猎人 价格=500").spec.price.min == 500


def test_parse_errors():
    with pytest.raises(DslError):
        parse_dsl("价格=100")
    with pytest.raises(DslError):
        parse_dsl("怪物猎人 颜色=红")
    with pytest.raises(DslError):
        parse_dsl("怪物猎人 价格=100-50")
    with pytest.raises(DslError):
        parse_dsl("怪物猎人 间隔=30s")


def test_parse_settings():
    fields = parse_settings("间隔=30m 条数=5 仅新=1 静默=23:00-08:00")
    assert fields == {
        "interval_sec": 1800,
        "max_push": 5,
        "only_new": 1,
        "quiet_hours": "23:00-08:00",
    }


def test_fingerprint_is_stable_and_order_free():
    a = parse_dsl("怪物猎人 价格=100-5000 品质=390-400").spec
    b = parse_dsl("怪物猎人 品质=390-400 价格=100-5000").spec
    assert fingerprint_for(a, ITEM) == fingerprint_for(b, ITEM)
    c = parse_dsl("怪物猎人 价格=100-5000 品质=390-410").spec
    assert fingerprint_for(a, ITEM) != fingerprint_for(c, ITEM)


def test_describe_spec():
    spec = parse_dsl("怪物猎人 价格=100-5000 品质=390-400 能力1=30-150").spec
    text = describe_spec(spec)
    assert "价格 100-5000" in text
    assert "品质 390-400" in text
    assert "能力1 30-150" in text


# ── 存储 ────────────────────────────────────────────────────


def test_store_crud_and_due(tmp_path):
    store = _store(tmp_path)
    sub_id = _add(store)
    sub = store.get_subscription(sub_id, 10001)
    assert sub is not None and sub.qq == 10001
    assert store.count_subscriptions(10001) == 1
    assert [s.id for s in store.due_subscriptions(int(time.time()))] == [sub_id]

    assert store.set_enabled(sub_id, 10001, False)
    assert store.due_subscriptions(int(time.time())) == []
    assert store.set_enabled(sub_id, 10001, True)

    assert store.delete_subscription(sub_id, 10001)
    assert store.get_subscription(sub_id, 10001) is None
    store.close()


def test_store_seen_new(tmp_path):
    store = _store(tmp_path)
    sub_id = _add(store)
    assert store.seen_new(sub_id, ["a", "b"]) == {"a", "b"}
    assert store.seen_new(sub_id, ["b", "c"]) == {"c"}
    store.close()


def test_store_ownership(tmp_path):
    store = _store(tmp_path)
    sub_id = _add(store, qq=10001)
    assert store.get_subscription(sub_id, 99999) is None
    assert not store.delete_subscription(sub_id, 99999)
    store.close()


# ── 调度 ────────────────────────────────────────────────────


def test_scheduler_shares_fetch_and_pushes(tmp_path):
    store = _store(tmp_path)
    _add(store, qq=10001, fingerprint="same")
    _add(store, qq=10002, fingerprint="same")
    market = FakeMarket()
    pusher = FakePusher()
    scheduler = Scheduler(store, market, pusher, cache_ttl=60)

    count = asyncio.run(scheduler.tick())
    assert count == 2
    assert market.calls == 1
    assert len([c for c in pusher.calls if "sub" in c]) == 2
    assert all(len(c["items"]) == 2 for c in pusher.calls if "sub" in c)

    sub = store.get_subscription(1, 10001)
    assert sub is not None and sub.last_count == 2
    assert sub.next_run_at > int(time.time())
    scheduler.close()
    store.close()


def test_scheduler_only_new_filters(tmp_path):
    store = _store(tmp_path)
    sid = _add(store, only_new=True)
    store.seen_new(sid, ["1"])  # 1 号已经见过
    market = FakeMarket()
    pusher = FakePusher()
    scheduler = Scheduler(store, market, pusher)

    asyncio.run(scheduler.tick())
    pushed = [c for c in pusher.calls if "sub" in c]
    assert len(pushed) == 1
    assert [it["n8Id"] for it in pushed[0]["items"]] == ["2"]
    scheduler.close()
    store.close()


def test_scheduler_first_run_base_pushes_nothing(tmp_path):
    store = _store(tmp_path)
    _add(store, first_push=False)
    market = FakeMarket()
    pusher = FakePusher()
    scheduler = Scheduler(store, market, pusher)

    asyncio.run(scheduler.tick())
    assert [c for c in pusher.calls if "sub" in c] == []
    sub = store.get_subscription(1, 10001)
    assert sub is not None and sub.last_count == 2
    scheduler.close()
    store.close()


def test_quiet_window():
    # 2026-10-22 23:30 CST
    ts = int(time.mktime(time.strptime("2026-10-22 23:30", "%Y-%m-%d %H:%M")))
    end = quiet_until("23:00-08:00", ts)
    assert end is not None and end > ts
    assert quiet_until("08:00-23:00", ts) is None


# ── 推送格式 ────────────────────────────────────────────────


def test_format_result_full_push_marks_new():
    sub = Subscription(
        id=1, qq=10001, spec={"item": {"name": "怪物猎人"}}, fingerprint="fp",
        interval_sec=600, next_run_at=0, enabled=True, only_new=False, max_push=20,
        quiet_hours="", first_push=True, fail_count=0, last_run_at=None,
        last_count=None, last_hash=None, created_at=0,
    )
    result = FetchResult(item=ITEM, resolved={}, items=[_raw("1", 600), _raw("2", 700)], requests=1)
    chunks = format_result(sub, result, {"1"})
    text = "\n".join(chunks)
    assert "本轮命中 2 条" in text
    assert "[新]" in text
    assert "600 卡利弗" in text
    assert "卖家 tester#1234" in text


def test_format_result_time_handles_short_millis():
    sub = Subscription(
        id=1, qq=10001, spec={"item": {"name": "怪物猎人"}}, fingerprint="fp",
        interval_sec=600, next_run_at=0, enabled=True, only_new=False, max_push=20,
        quiet_hours="", first_push=True, fail_count=0, last_run_at=None,
        last_count=None, last_hash=None, created_at=0,
    )
    raw = _raw("1", 600)
    raw["dtRegister"] = "2026-10-22T10:56:09.72"
    result = FetchResult(item=ITEM, resolved={}, items=[raw], requests=1)
    text = "\n".join(format_result(sub, result, set()))
    assert "10-22 18:56" in text


def test_format_result_caps_and_summarizes():
    sub = Subscription(
        id=1, qq=10001, spec={"item": {"name": "怪物猎人"}}, fingerprint="fp",
        interval_sec=600, next_run_at=0, enabled=True, only_new=False, max_push=1,
        quiet_hours="", first_push=True, fail_count=0, last_run_at=None,
        last_count=None, last_hash=None, created_at=0,
    )
    result = FetchResult(
        item=ITEM, resolved={}, items=[_raw(str(i), 100 + i) for i in range(3)], requests=1
    )
    text = "\n".join(format_result(sub, result, set()))
    assert "其余 2 条未列出" in text


class RecordingClient:
    """记录 send_private_msg 的消息段，供 Pusher 测试使用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, list[dict]]] = []

    def send_private_msg(self, qq, message):
        self.calls.append((qq, message))
        return {}


def _push_sub(max_push: int = 20) -> Subscription:
    return Subscription(
        id=1, qq=10001, spec={"item": {"name": "怪物猎人"}}, fingerprint="img-fp",
        interval_sec=600, next_run_at=0, enabled=True, only_new=False, max_push=max_push,
        quiet_hours="", first_push=True, fail_count=0, last_run_at=None,
        last_count=None, last_hash=None, created_at=0,
    )


def test_pusher_sends_image_with_short_caption(tmp_path):
    from qqbot.pusher import Pusher

    client = RecordingClient()
    pusher = Pusher(
        client, image_enabled=True, image_style="hybrid", render_dir=tmp_path
    )
    result = FetchResult(
        item=ITEM, resolved={}, items=[_raw("1", 600), _raw("2", 700)], requests=1
    )
    asyncio.run(pusher.push(_push_sub(), result, {"1"}))

    assert len(client.calls) == 1
    segments = client.calls[0][1]
    assert segments[0]["type"] == "text"
    caption = segments[0]["data"]["text"]
    assert "本轮命中 2 条" in caption and "[新]" not in caption
    assert segments[1]["type"] == "image"
    assert segments[1]["data"]["file"].startswith("base64://")
    assert len(segments) == 2


def test_pusher_falls_back_to_text_when_render_fails(tmp_path, monkeypatch):
    import qqbot.pusher as pusher_mod
    from qqbot.pusher import Pusher

    def boom(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr(pusher_mod, "render_card", boom)
    client = RecordingClient()
    pusher = Pusher(client, image_enabled=True, render_dir=tmp_path)
    result = FetchResult(item=ITEM, resolved={}, items=[_raw("1", 600)], requests=1)
    asyncio.run(pusher.push(_push_sub(), result, {"1"}))

    assert len(client.calls) == 1
    segments = client.calls[0][1]
    assert segments[0]["type"] == "text"
    assert "本轮命中 1 条" in segments[0]["data"]["text"]
    assert "600 卡利弗" in segments[0]["data"]["text"]


def test_pusher_text_only_when_image_disabled(tmp_path):
    from qqbot.pusher import Pusher

    client = RecordingClient()
    pusher = Pusher(client, image_enabled=False, render_dir=tmp_path)
    result = FetchResult(item=ITEM, resolved={}, items=[_raw("1", 600)], requests=1)
    asyncio.run(pusher.push(_push_sub(), result, {"1"}))
    assert all(seg["type"] == "text" for _, msg in client.calls for seg in msg)


# ── 向导 ────────────────────────────────────────────────────


def test_wizard_flow():
    wizard = Wizard(lambda q: ("怪物猎人", "超越武器"))
    assert "1/4" in wizard.start(10001).text
    assert "2/4" in wizard.feed(10001, "怪物猎人").text
    reply = wizard.feed(10001, "100-5000")
    assert "3/4" in reply.text
    reply = wizard.feed(10001, "跳过")
    assert "4/4" in reply.text
    reply = wizard.feed(10001, "10m")
    assert reply.done and reply.dsl == "/订阅 怪物猎人 价格=100-5000 间隔=10m"
    assert not wizard.active(10001)


def test_wizard_rejects_bad_price():
    wizard = Wizard(lambda q: ("怪物猎人", "超越武器"))
    wizard.start(10001)
    wizard.feed(10001, "怪物猎人")
    reply = wizard.feed(10001, "一百块")
    assert "价格不合法" in reply.text
    assert wizard.active(10001)


def test_wizard_purges_expired_sessions():
    wizard = Wizard(lambda q: ("怪物猎人", "超越武器"))
    wizard.start(10001)
    wizard.start(10002)
    assert wizard.purge_expired() == 0
    wizard._states[10001].updated -= 700
    assert wizard.purge_expired() == 1
    assert not wizard.active(10001)
    assert wizard.active(10002)


def test_store_prunes_old_runs(tmp_path):
    store = _store(tmp_path)
    sid = _add(store)
    old = int(time.time()) - 40 * 86400
    store.record_success(
        sid, started_at=old, matched=1, pushed=1, new_count=1, requests=1,
        last_hash="x", next_run_at=0,
    )
    assert len(store.recent_runs(sid)) == 1
    assert store.prune_runs(days=30) == 1
    assert store.recent_runs(sid) == []
    store.close()


def test_scheduler_prunes_cache(tmp_path):
    store = _store(tmp_path)
    scheduler = Scheduler(store, FakeMarket(), FakePusher())
    scheduler._cache["old"] = (time.time() - 1000, None)
    scheduler._cache["new"] = (time.time(), None)
    assert scheduler.prune_cache(max_entries=200, max_age=600) == 1
    assert list(scheduler._cache) == ["new"]
    scheduler._cache.clear()
    scheduler._cache["a"] = (time.time() - 2, None)
    scheduler._cache["b"] = (time.time() - 1, None)
    scheduler._cache["c"] = (time.time(), None)
    scheduler.prune_cache(max_entries=1, max_age=600)
    assert list(scheduler._cache) == ["c"]
    scheduler.close()
    store.close()


def test_group_whitelist_parser():
    from run_bot import _parse_groups

    assert _parse_groups("123, 456，789") == {123, 456, 789}
    assert _parse_groups("") == set()


def test_config_file_overrides_subscription_limit(tmp_path):
    from run_bot import _load_config, _positive_int

    path = tmp_path / "config.json"
    path.write_text('{"max_subscriptions": 20}', encoding="utf-8")
    assert _positive_int(_load_config(path).get("max_subscriptions"), 5) == 20

    path.write_text('{"max_subscriptions": "20"}', encoding="utf-8")
    assert _positive_int(_load_config(path).get("max_subscriptions"), 5) == 20

    path.write_text('{"max_subscriptions": 0}', encoding="utf-8")
    assert _positive_int(_load_config(path).get("max_subscriptions"), 5) == 5

    path.write_text("not json", encoding="utf-8")
    assert _load_config(path) == {}
    assert _load_config(tmp_path / "missing.json") == {}


def test_subscription_limit_comes_from_settings(tmp_path):
    bot, sent = _make_bot(tmp_path, max_subscriptions=1)
    try:
        _add(bot.store, qq=10001)
        asyncio.run(bot._handle_command(10001, 0, 0, "订阅 怪物猎人 价格=100-5000"))
        assert "最多只能创建 1 个订阅" in _last_text(sent)
    finally:
        bot.scheduler.close(); bot.store.close()


def test_store_finds_duplicate_subscription(tmp_path):
    store = _store(tmp_path)
    sid = _add(store, qq=10001, fingerprint="same")
    found = store.find_by_fingerprint(10001, "same")
    assert found is not None and found.id == sid
    assert store.find_by_fingerprint(10002, "same") is None
    assert store.find_by_fingerprint(10001, "other") is None
    store.close()


class FlakyPusher(FakePusher):
    def __init__(self):
        super().__init__()
        self.fail = True

    async def push(self, sub, result, new_ids, *, items=None, same_as_last=False):
        if self.fail:
            raise RuntimeError("send failed")
        return await super().push(
            sub, result, new_ids, items=items, same_as_last=same_as_last
        )


def test_transport_failure_backoff_is_capped(tmp_path):
    """NapCat 掉线造成的推送失败最多退避 5 分钟。"""
    store = _store(tmp_path)
    sid = _add(store)
    scheduler = Scheduler(store, FakeMarket(), FlakyPusher())
    now = int(time.time())

    asyncio.run(scheduler.tick())
    sub = store.get_subscription(sid)
    assert sub.fail_count == 1
    assert 60 <= sub.next_run_at - now <= 300
    scheduler.close()
    store.close()


def test_fetch_failure_keeps_exponential_backoff(tmp_path):
    """市场接口失败仍按指数退避，不受 5 分钟上限影响。"""

    class BrokenMarket:
        def fetch(self, spec, max_pages=3):
            raise RuntimeError("HTTP 403")

    store = _store(tmp_path)
    sid = _add(store)
    scheduler = Scheduler(store, BrokenMarket(), FakePusher())
    now = int(time.time())

    asyncio.run(scheduler.tick())
    sub = store.get_subscription(sid)
    assert sub.fail_count == 1
    assert sub.next_run_at - now >= 1100
    scheduler.close()
    store.close()


def test_recover_stale_backoff(tmp_path):
    store = _store(tmp_path)
    sid = _add(store)
    now = int(time.time())
    store.record_failure(
        sid, started_at=now, error="NapCatError: down",
        next_run_at=now + 3600, fail_count=3,
    )

    assert store.recover_stale_backoff(now) == 1
    sub = store.get_subscription(sid)
    assert sub.fail_count == 0
    assert sub.next_run_at <= now + 60
    store.close()


def test_scheduler_start_clears_stale_backoff(tmp_path):
    store = _store(tmp_path)
    sid = _add(store)
    now = int(time.time())
    store.record_failure(
        sid, started_at=now, error="NapCatError: down",
        next_run_at=now + 3600, fail_count=3,
    )
    scheduler = Scheduler(store, FakeMarket(), FakePusher())

    async def fake_tick():
        return 0

    scheduler.tick = fake_tick  # type: ignore[assignment]

    async def run_once():
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_once())
    sub = store.get_subscription(sid)
    assert sub.fail_count == 0
    assert sub.next_run_at <= int(time.time()) + 60
    scheduler.close()
    store.close()


def test_seen_marked_only_after_successful_push(tmp_path):
    store = _store(tmp_path)
    sid = _add(store, only_new=True)
    pusher = FlakyPusher()
    scheduler = Scheduler(store, FakeMarket(), pusher)

    asyncio.run(scheduler.tick())
    # 推送失败：不能把商品标记为已见，否则“只推新”会永久丢消息
    assert store.check_new(sid, ["1", "2"]) == {"1", "2"}
    assert store.get_subscription(sid).fail_count == 1

    store.reschedule(sid, 0)
    pusher.fail = False
    asyncio.run(scheduler.tick())
    assert len([c for c in pusher.calls if "sub" in c]) == 1
    assert store.check_new(sid, ["1", "2"]) == set()
    scheduler.close()
    store.close()


def test_single_instance_lock(tmp_path):
    from run_bot import _acquire_lock, _release_lock

    path = str(tmp_path / "bot.lock")
    first = _acquire_lock(path)
    assert first is not None
    assert _acquire_lock(path) is None
    _release_lock(first)
    again = _acquire_lock(path)
    assert again is not None
    _release_lock(again)


def test_render_card_and_cache(tmp_path):
    from qqbot.card import render_card

    sub = Subscription(
        id=1, qq=10001, spec={"item": {"name": "怪物猎人"}}, fingerprint="card-fp",
        interval_sec=600, next_run_at=0, enabled=True, only_new=False, max_push=2,
        quiet_hours="", first_push=True, fail_count=0, last_run_at=None,
        last_count=None, last_hash=None, created_at=0,
    )
    result = FetchResult(
        item=ITEM, resolved={}, items=[_raw("1", 600), _raw("2", 700)], requests=1
    )
    path = render_card(result, sub, {"1"}, out_dir=tmp_path)
    assert path.exists() and path.stat().st_size > 1000
    assert render_card(result, sub, {"1"}, out_dir=tmp_path) == path


def test_render_card_styles(tmp_path):
    """三种版式都能画完整版超越武器，且缓存键按版式区分。"""
    from qqbot.card import render_card

    raw = _raw("9", 12000)
    raw["seller"]["n4MasteryRank"] = 36
    raw["data"] = {
        "n4WeaponQuality": 400,
        "strPerkName": "猎人感知",
        "strClassName": "突击步枪",
        "strBulletName": "普通弹",
        "subperks": [
            {"strName": "巨魔强化弹头 I", "isFixed": 0,
             "options": [{"strDesc": "枪械攻击力\t58.2%[x]"}]},
            {"strName": "属性改换：火焰", "isFixed": 1, "options": []},
        ],
        "options": [
            {"strDesc": "连环爆炸\n范围\t半径14.44米 (143pt)", "n4Point": "143", "n4Group": 0},
            {"strDesc": "连环爆炸\n额外伤害量\t枪械伤害量ｘ954%", "n4Point": "143", "n4Group": 0},
            {"strDesc": "跳弹\n额外伤害量\t枪械伤害量ｘ100% (3pt)", "n4Point": "3", "n4Group": 1},
            {"strDesc": "怪物猎人\n额外伤害量\t枪械伤害量ｘ50% (4pt)", "n4Point": "4", "n4Group": 2},
            {"strDesc": "装配时的应用效果\n对巨魔攻击时获得额外攻击力\t8,441",
             "n4Point": "4", "n4Group": 2},
        ],
    }
    sub = Subscription(
        id=1, qq=10001, spec={"item": {"name": "怪物猎人"}}, fingerprint="style-fp",
        interval_sec=600, next_run_at=0, enabled=True, only_new=False, max_push=5,
        quiet_hours="", first_push=True, fail_count=0, last_run_at=None,
        last_count=None, last_hash=None, created_at=0,
    )
    result = FetchResult(item=ITEM, resolved={}, items=[raw], requests=1)
    paths = {
        style: render_card(result, sub, {"9"}, style=style, out_dir=tmp_path)
        for style in ("official", "compact", "hybrid")
    }
    assert len(set(paths.values())) == 3
    for style, path in paths.items():
        assert path.exists() and path.stat().st_size > 3000, style

    with pytest.raises(ValueError):
        render_card(result, sub, set(), style="unknown", out_dir=tmp_path)


def test_render_card_module_and_trigger(tmp_path):
    """模块与触发器的词条解析正确，三种版式都能出图。"""
    from qqbot.card import _build_listing, render_card

    module_raw = {
        "n8Id": "77",
        "n4SalePrice": "100",
        "dtRegister": "2026-11-02T14:57:50.044",
        "seller": {"strNickName": "tester#1234"},
        "data": {
            "strSocketName": "金红石",
            "options": [
                {"strDesc": "无属性技能威力增加率 [15.2~57.5]%\t22.8%[+]\\s24%[+]",
                 "strTier": "Tier1", "strSlot": "Advantage"},
                {"strDesc": "技能暴击倍率 [46.1~174.1]%\t95.9%[x]\\s101.3%[x]",
                 "strTier": "Tier2", "strSlot": "Advantage"},
                {"strDesc": "最大护盾 [-94.3~-25]%\t-90%[x]\\s-84.3%[x]",
                 "strTier": "None", "strSlot": "Penalty"},
            ],
        },
    }
    listing = _build_listing(module_raw, set(), "ancestor")
    assert listing.socket_name == "金红石"
    assert [a.tier for a in listing.advantages] == ["Tier1", "Tier2"]
    assert listing.advantages[0].value == "22.8%+ / 24%+"
    assert listing.advantages[1].range_text == "46.1 ~ 174.1"
    assert listing.penalties[0].label == "最大护盾"
    assert listing.penalties[0].value == "-90%× / -84.3%×"

    trigger_raw = {
        "n8Id": "88",
        "n4SalePrice": "50",
        "dtRegister": "2026-11-02T14:44:03.508",
        "seller": {"strNickName": "tester#1234"},
        "data": {
            "options": [
                {"strDesc": "4号技能应用冷却时间减少率(25.0 ~ 100.0)\\t96%", "strTier": "Tier3"},
                {"strDesc": "毒属性技能威力增加量(10.0 ~ 50.0)\\t31%", "strTier": "Tier2"},
            ],
        },
    }
    trigger = _build_listing(trigger_raw, set(), "trigger")
    assert [e.value for e in trigger.trigger_effects] == ["96%", "31%"]
    assert trigger.trigger_effects[0].label == "4号技能应用冷却时间减少率"
    assert trigger.trigger_effects[0].range_text == "25.0 ~ 100.0"

    module_item = CatalogItem(
        item_id="261001023", item_type="ancestor", names={"cn": "阴影的决意"}
    )
    trigger_item = CatalogItem(
        item_id="255000045", item_type="trigger", names={"cn": "暗中重整"}
    )
    cases = ((module_item, module_raw), (trigger_item, trigger_raw))
    for item, raw in cases:
        sub = Subscription(
            id=1, qq=10001, spec={"item": {"name": item.name("cn")}},
            fingerprint=f"detail-{item.item_id}", interval_sec=600, next_run_at=0,
            enabled=True, only_new=False, max_push=1, quiet_hours="", first_push=True,
            fail_count=0, last_run_at=None, last_count=None, last_hash=None, created_at=0,
        )
        result = FetchResult(item=item, resolved={}, items=[raw], requests=1)
        for style in ("official", "compact", "hybrid"):
            path = render_card(result, sub, {"9"}, style=style, out_dir=tmp_path)
            assert path.exists() and path.stat().st_size > 2000, (item.item_type, style)
            path.unlink()


def test_asset_index_lookup(tmp_path, monkeypatch):
    import qqbot.assets as assets

    items = tmp_path / "items"
    items.mkdir()
    image = items / "123_abc.png"
    image.write_bytes(b"fake")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"123": {"url": "u", "path": "items/123_abc.png"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(assets, "ROOT", tmp_path)
    monkeypatch.setattr(assets, "INDEX_PATH", index)

    assert assets.icon_path("123") == image
    assert assets.icon_path("999") is None


def _query_bot(tmp_path, item_type="transcendent", name="怪物猎人"):
    bot, sent = _make_bot(tmp_path)
    item = CatalogItem(item_id="211064002", item_type=item_type, names={"cn": name})
    bot.market.resolve = lambda q, t=None: item
    raws = [_raw("1", 900), _raw("2", 300), _raw("3", 500), _raw("4", 100)]
    calls: dict = {}

    def fetch(spec, max_pages=3):
        calls["spec"] = spec
        calls["max_pages"] = max_pages
        return FetchResult(item=item, resolved={}, items=raws, requests=1)

    bot.market.fetch = fetch
    return bot, sent, calls


def _last_text(sent: list[dict]) -> str:
    for seg in reversed(sent[-1]["segs"]):
        if seg.get("type") == "text":
            return seg["data"]["text"]
    return ""


def test_price_query_overview(tmp_path):
    bot, sent, _ = _query_bot(tmp_path)
    try:
        asyncio.run(bot._handle_command(10001, 0, 0, "查价"))
        text = _last_text(sent)
        assert "超越武器" in text
        assert "模块/继承者" in text
        assert "触发器" in text
        assert "不落库" in text
    finally:
        bot.scheduler.close(); bot.store.close()


def test_price_query_name_only_shows_type_template(tmp_path):
    bot, sent, _ = _query_bot(tmp_path)
    try:
        asyncio.run(bot._handle_command(10001, 0, 0, "查价 怪物猎人"))
        text = _last_text(sent)
        assert "超越武器价格查询模板" in text
        assert "品质=390-400" in text
        assert "能力1=30-150" in text
    finally:
        bot.scheduler.close(); bot.store.close()


def test_price_query_ancestor_template_has_no_quality(tmp_path):
    bot, sent, _ = _query_bot(tmp_path, item_type="ancestor", name="阴影的决意")
    try:
        asyncio.run(bot._handle_command(10001, 0, 0, "查价 阴影的决意"))
        text = _last_text(sent)
        assert "模块价格查询模板" in text
        assert "增益=" in text and "减益=" in text
        assert "品质" not in text
    finally:
        bot.scheduler.close(); bot.store.close()


def test_price_query_returns_three_lowest(tmp_path):
    bot, sent, calls = _query_bot(tmp_path)
    try:
        asyncio.run(
            bot._handle_command(10001, 0, 0, "查价 怪物猎人 价格=50-1000")
        )
        text = _last_text(sent)
        assert "最低价前 3 条" in text
        assert any(seg.get("type") == "image" for seg in sent[-1]["segs"])
        assert calls["spec"].sort == "price_asc"
        assert calls["max_pages"] == 1
        # 价格查询不落库
        assert bot.store.count_subscriptions(10001) == 0
        assert bot.store.list_purchases(10001) == []
    finally:
        bot.scheduler.close(); bot.store.close()


def test_price_query_rejects_unknown_key(tmp_path):
    bot, sent, _ = _query_bot(tmp_path)
    try:
        asyncio.run(bot._handle_command(10001, 0, 0, "查价 怪物猎人 颜色=红"))
        text = _last_text(sent)
        assert "条件格式错误" in text
        assert "查看该类型模板" in text
    finally:
        bot.scheduler.close(); bot.store.close()


def test_price_query_type_mismatch_shows_template(tmp_path):
    bot, sent, _ = _query_bot(tmp_path, item_type="ancestor", name="阴影的决意")
    try:
        asyncio.run(bot._handle_command(10001, 0, 0, "查价 阴影的决意 品质=400"))
        text = _last_text(sent)
        assert "条件不适用于" in text
        assert "增益=" in text
    finally:
        bot.scheduler.close(); bot.store.close()


def _make_bot(tmp_path, **settings):
    from qqbot.bot import BotSettings, QQBot

    bot = QQBot(BotSettings(db_path=str(tmp_path / "bot.db"), token="", **settings))
    bot.self_id = 3964200143
    sent: list[dict] = []
    bot.client.send_group_msg = lambda gid, segs: sent.append({"group": gid, "segs": segs})
    bot.client.send_private_msg = lambda qq, segs: sent.append({"qq": qq, "segs": segs})
    return bot, sent


def _group_event(text, *, uid=10001, gid=20002, mid=1, at_self=False):
    message = []
    if at_self:
        message.append({"type": "at", "data": {"qq": "3964200143"}})
    message.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message", "message_type": "group", "self_id": 3964200143,
        "user_id": uid, "group_id": gid, "message_id": mid,
        "message": message, "sender": {"user_id": uid},
    }


def test_check_now_from_group_goes_private_only(tmp_path):
    bot, sent = _make_bot(tmp_path)
    sub_id = _add(bot.store, qq=10001)

    async def fake_run_one(sub):
        return 3, None

    bot.scheduler.run_one = fake_run_one
    try:
        asyncio.run(bot._handle_command(10001, 20002, 11, f"立即查 {sub_id}"))
        assert sent, "应发送检查状态"
        assert all("qq" in item for item in sent)
        assert not any("group" in item for item in sent)
    finally:
        bot.scheduler.close(); bot.store.close()


def test_group_reply_never_at_sender(tmp_path):
    bot, sent = _make_bot(tmp_path)
    bot._last_command.clear()
    asyncio.run(bot.handle_event(_group_event("/帮助", mid=11)))
    assert sent, "应当回复 /帮助"
    types = [seg["type"] for seg in sent[0]["segs"]]
    assert "at" not in types
    assert types[0] == "reply" and "text" in types
    bot.scheduler.close(); bot.store.close()


def test_bare_mention_is_ignored(tmp_path):
    bot, sent = _make_bot(tmp_path)
    asyncio.run(bot.handle_event(_group_event("你好", mid=12, at_self=True)))
    assert sent == []
    bot.scheduler.close(); bot.store.close()


def test_ignore_qq_blocks_bot(tmp_path):
    bot, sent = _make_bot(tmp_path, ignore_qq=frozenset({999999}))
    asyncio.run(bot.handle_event(_group_event("/帮助", uid=999999, mid=13)))
    assert sent == []
    bot.scheduler.close(); bot.store.close()


def test_group_loop_guard_mutes_sender(tmp_path):
    from qqbot.bot import GROUP_REPLY_WINDOW

    bot, _ = _make_bot(tmp_path)
    for _ in range(GROUP_REPLY_WINDOW):
        bot._mark_group_reply(20002, 10001)
    assert not bot._group_allowed(20002, 10001)
    assert bot._group_allowed(20002, 10002)
    bot.scheduler.close(); bot.store.close()



