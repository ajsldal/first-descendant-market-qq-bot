"""bot 侧权限读取与拦截测试。"""

from __future__ import annotations

import asyncio
import time

from admin.db import BotDB
from market_v2.models import CatalogItem
from qqbot import bot as botmod
from qqbot.bot import BotSettings, QQBot
from qqbot.market import FetchResult
from qqbot.permissions import PermissionService
from qqbot.scheduler import Scheduler
from qqbot.steam_vault import SteamVault
from qqbot.store import Store

ITEM = CatalogItem(
    item_id="211064002", item_type="transcendent", names={"cn": "怪物猎人"}
)


def _raw(n8: str = "1") -> dict:
    return {
        "n8Id": n8,
        "n4SalePrice": "600",
        "dtRegister": "2026-10-27T10:00:00.000",
        "seller": {"strNickName": "seller#1"},
        "data": {
            "n4WeaponQuality": 400,
            "subperks": [
                {"strName": "A", "isFixed": 0, "options": [{"strDesc": "a"}]},
                {"strName": "B", "isFixed": 0, "options": [{"strDesc": "b"}]},
            ],
            "options": [
                {"strDesc": "g", "n4Point": "10", "n4Group": 0},
                {"strDesc": "g", "n4Point": "20", "n4Group": 1},
                {"strDesc": "g", "n4Point": "30", "n4Group": 2},
            ],
        },
    }


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


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_private_msg(self, qq, segments):
        self.calls.append({"qq": qq, "segments": segments})
        return {}

    def send_group_msg(self, group_id, segments):
        self.calls.append({"group": group_id, "segments": segments})
        return {}


def _make_bot(tmp_path) -> QQBot:
    bot = QQBot(
        BotSettings(db_path=str(tmp_path / "bot.db"), token="x", image_enabled=False)
    )
    bot.self_id = 3964200143
    bot.client = FakeClient()
    return bot


def _event(text: str, *, uid: int = 10001, mid: int = 1) -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 3964200143,
        "user_id": uid,
        "message_id": mid,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": uid},
    }


def test_permission_service_reads_admin_tables(tmp_path):
    store = Store(tmp_path / "bot.db")
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        service = PermissionService(
            store,
            defaults={
                "max_subscriptions": 5,
                "analytics_enabled": True,
                "analytics_whitelist_qq": [],
                "analytics_runs_retention_days": 30,
            },
        )
        assert service.max_subscriptions(10001) == 5
        assert service.features(10001)["allow_trade"] is True
        assert service.is_blocked(10001) is False

        admin_db.update_settings(
            {
                "max_subscriptions": 7,
                "analytics_enabled": False,
                "analytics_whitelist_qq": [10001],
                "analytics_runs_retention_days": 12,
                "steam_auto_login_enabled": False,
                "steam_auto_allowed_qq": [10001],
                "login_allowed_qq": [10001],
                "steam_auto_guard_timeout": 900,
            },
            admin_id=0,
        )
        admin_db.update_permissions(
            10001, {"allow_trade": False, "max_subscriptions": 3}, admin_id=0
        )
        service.invalidate()

        assert service.max_subscriptions(10001) == 3
        assert service.features(10001)["allow_trade"] is False
        assert service.analytics_enabled() is False
        assert service.analytics_whitelist() == frozenset({10001})
        assert service.analytics_retention_days() == 12
        assert service.steam_auto_login_enabled() is False
        assert service.steam_auto_allowed_qq() == frozenset({10001})
        assert service.login_allowed_qq() == frozenset({10001})
        assert service.login_allowed(10001) is True
        assert service.login_allowed(10002) is False
        assert service.steam_auto_guard_timeout() == 900

        admin_db.block_user(10001, reason="test", until=None, admin_id=0)
        service.invalidate(10001)
        assert service.is_blocked(10001) is True

        admin_db.block_user(
            10001, reason="expired", until=int(time.time()) - 1, admin_id=0
        )
        service.invalidate(10001)
        assert service.is_blocked(10001) is False
    finally:
        admin_db.close()
        store.close()


def test_blocked_user_is_ignored(tmp_path):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        admin_db.block_user(10001, reason="test", until=None, admin_id=0)
        bot.permissions.invalidate(10001)
        asyncio.run(bot.handle_event(_event("/帮助")))
        assert bot.client.calls == []
    finally:
        admin_db.close()
        bot.scheduler.close()
        bot.store.close()


def test_feature_flag_blocks_subscribe(tmp_path):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        admin_db.update_permissions(10001, {"allow_subscribe": False}, admin_id=0)
        bot.permissions.invalidate(10001)
        asyncio.run(
            bot._handle_command(10001, 0, 0, "订阅 怪物猎人 价格=100-5000")
        )
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "没有创建订阅的权限" in text
    finally:
        admin_db.close()
        bot.scheduler.close()
        bot.store.close()


def test_per_user_subscription_limit(tmp_path):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        _add_sub(bot.store)
        admin_db.update_settings({"max_subscriptions": 1}, admin_id=0)
        bot.permissions.invalidate()
        asyncio.run(
            bot._handle_command(10001, 0, 0, "订阅 怪物猎人 价格=100-5000")
        )
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "最多只能创建 1 个订阅" in text
    finally:
        admin_db.close()
        bot.scheduler.close()
        bot.store.close()


def test_login_commands_require_login_whitelist(tmp_path):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        admin_db.update_settings({"login_allowed_qq": [10001]}, admin_id=0)
        bot.permissions.invalidate()

        asyncio.run(bot._handle_command(10002, 0, 0, "登录状态"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "白名单" in text

        # 登出只删本地凭据，不受白名单限制
        asyncio.run(bot._handle_command(10002, 0, 0, "登出"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "没有登录态" in text

        asyncio.run(bot._handle_command(10001, 0, 0, "登录状态"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "还没有登录" in text
    finally:
        admin_db.close()
        bot.scheduler.close()
        bot.store.close()


def test_auto_login_requires_whitelist(tmp_path):
    bot = _make_bot(tmp_path)
    try:
        asyncio.run(bot._auto_login(10001, 0, 0))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "白名单" in text
    finally:
        bot.scheduler.close()
        bot.store.close()


def test_auto_login_alias_routes(tmp_path):
    bot = _make_bot(tmp_path)
    try:
        asyncio.run(bot._handle_command(10001, 0, 0, "自动登录"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "白名单" in text
    finally:
        bot.scheduler.close()
        bot.store.close()


def test_auto_login_respects_switch(tmp_path):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        admin_db.update_settings(
            {
                "steam_auto_login_enabled": False,
                "steam_auto_allowed_qq": [10001],
                "login_allowed_qq": [10001],
            },
            admin_id=0,
        )
        bot.permissions.invalidate()
        asyncio.run(bot._auto_login(10001, 0, 0))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "关闭" in text
    finally:
        admin_db.close()
        bot.scheduler.close()
        bot.store.close()


def test_auto_login_requires_vault(tmp_path, monkeypatch):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        admin_db.update_settings(
            {
                "steam_auto_login_enabled": True,
                "steam_auto_allowed_qq": [10001],
                "login_allowed_qq": [10001],
            },
            admin_id=0,
        )
        bot.permissions.invalidate()
        monkeypatch.setattr(
            botmod, "default_vault", SteamVault(tmp_path / "missing_vault.bin")
        )
        asyncio.run(bot._auto_login(10001, 0, 0))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "还没有配置" in text
    finally:
        admin_db.close()
        bot.scheduler.close()
        bot.store.close()


def test_scheduler_skips_blocked_user(tmp_path):
    class FakeMarket:
        def fetch(self, spec, max_pages=3):
            return FetchResult(
                item=ITEM, resolved={}, items=[_raw()], requests=1, complete=True
            )

    class FakePusher:
        def __init__(self):
            self.called = False

        async def push(self, sub, result, new_ids, *, items=None, same_as_last=False):
            self.called = True
            return 1

    store = Store(tmp_path / "bot.db")
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        _add_sub(store)
        admin_db.block_user(10001, reason="test", until=None, admin_id=0)
        permissions = PermissionService(store)
        pusher = FakePusher()
        scheduler = Scheduler(
            store, FakeMarket(), pusher, permissions=permissions
        )

        asyncio.run(scheduler.tick())
        assert pusher.called is False
        sub = store.get_subscription(1)
        assert sub is not None and sub.last_count == 1
        scheduler.close()
    finally:
        admin_db.close()
        store.close()
