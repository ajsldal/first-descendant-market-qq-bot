"""好友缓存、订阅好友巡检与投递失败自动暂停测试。"""

from __future__ import annotations

import asyncio

import pytest

from market_v2.models import CatalogItem
from qqbot.friends import FriendDirectory
from qqbot.market import FetchResult
from qqbot.napcat_client import ApiResponse, NapCatError
from qqbot.scheduler import Scheduler
from qqbot.store import Store

ITEM = CatalogItem(
    item_id="211064002", item_type="transcendent", names={"cn": "怪物猎人"}
)


class FakeFriendClient:
    def __init__(self, ids, *, fail: bool = False) -> None:
        self.ids = set(ids)
        self.fail = fail

    def get_friend_list(self):
        if self.fail:
            raise RuntimeError("napcat offline")
        return [{"user_id": qq} for qq in self.ids]


class FakeMarket:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, spec, max_pages=3):
        self.calls += 1
        return FetchResult(item=ITEM, resolved={}, items=[], requests=1, complete=True)


class FakePusher:
    def __init__(self) -> None:
        self.pushed = 0
        self.notified = 0

    async def push(self, sub, result, new_ids, *, items=None, same_as_last=False):
        self.pushed += 1
        return 0

    async def send_private(self, qq, content):
        self.notified += 1


def _add_sub(store: Store, qq: int = 10001) -> int:
    return store.add_subscription(
        qq,
        {"item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"}},
        "fp1",
        interval_sec=600,
        max_push=20,
        only_new=False,
        quiet_hours="",
        first_push=True,
        next_run_at=0,
    )


def test_friend_directory_cache_and_add():
    client = FakeFriendClient({10001})
    directory = FriendDirectory(lambda: client)
    assert directory.is_friend(10001) is True
    assert directory.is_friend(10002) is False
    directory.add(10002)
    assert directory.is_friend(10002, refresh=False) is True


def test_friend_directory_unknown_on_failure():
    client = FakeFriendClient(set(), fail=True)
    directory = FriendDirectory(lambda: client)
    assert directory.is_friend(10001) is None


def test_api_response_requires_ok():
    ApiResponse(retcode=0, status="ok").require_ok()
    with pytest.raises(NapCatError):
        ApiResponse(retcode=100, status="failed", message="blocked").require_ok()


def test_scheduler_pauses_subscription_for_non_friend(tmp_path):
    store = Store(tmp_path / "bot.db")
    market = FakeMarket()
    pusher = FakePusher()
    friends = FriendDirectory(lambda: FakeFriendClient({10001}))
    scheduler = Scheduler(store, market, pusher, friends=friends)  # type: ignore[arg-type]
    try:
        sub_id = _add_sub(store, qq=10002)
        assert asyncio.run(scheduler.tick()) == 0
        sub = store.get_subscription(sub_id)
        assert sub is not None and sub.enabled == 0
        assert market.calls == 0
        assert pusher.pushed == 0
    finally:
        scheduler.close()
        store.close()


def test_scheduler_auto_pauses_after_transport_failures(tmp_path):
    store = Store(tmp_path / "bot.db")
    pusher = FakePusher()
    scheduler = Scheduler(store, FakeMarket(), pusher)  # type: ignore[arg-type]
    try:
        sub_id = _add_sub(store)
        current = store.get_subscription(sub_id)
        assert current is not None

        async def fail_ten_times() -> None:
            for _ in range(10):
                sub = store.get_subscription(sub_id)
                assert sub is not None
                scheduler._record_failure(sub, RuntimeError("send failed"), transport=True)
                await asyncio.sleep(0)

        asyncio.run(fail_ten_times())
        sub = store.get_subscription(sub_id)
        assert sub is not None
        assert sub.fail_count == 10
        assert sub.enabled == 0
    finally:
        scheduler.close()
        store.close()
