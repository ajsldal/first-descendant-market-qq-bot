from __future__ import annotations

import asyncio

from market_v2.models import CatalogItem
from qqbot import bot as botmod
from qqbot.bot import QQBot, BotSettings
from qqbot.card import _build_listing
from qqbot.market import FetchResult
from qqbot.trade import TradeResult
from tests.fakes import make_raw


class FakeClient:
    def send_private_msg(self, qq, segments):
        return {}

    def send_group_msg(self, gid, segments):
        return {}


def _raw(n8: str, price: float, *, online: int | None = 1) -> dict:
    raw = make_raw(n8, price, quality=400)
    if online is None:
        raw["seller"].pop("isAccessState", None)
    else:
        raw["seller"]["isAccessState"] = online
    return raw


def _bot(monkeypatch, *, chat: TradeResult | None = None, confirm: TradeResult | None = None):
    class FakeTrade:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, n8, item_type, item_id, lounge=False):
            return chat or TradeResult(
                step="chat", code="SUCCESS", seller="seller#1", enc="ENC"
            )

        def confirm(self, enc, lounge=False):
            return confirm or TradeResult(step="chat/buy", code="SUCCESS")

        def close(self):
            pass

    monkeypatch.setattr(botmod, "TradeClient", FakeTrade)
    bot = QQBot(BotSettings(db_path=":memory:", token="x", image_enabled=False))
    bot.client = FakeClient()
    bot.store.save_token(123, {"_if_game_auth": "a", "_ifwt_guest_2392": "b"}, status="valid")
    item = CatalogItem(
        item_id="211064002", item_type="transcendent", names={"cn": "怪物猎人"}
    )
    bot.market.resolve = lambda q, t=None: item
    raws = [
        _raw("898484701286067202", 1050),
        _raw("898484701286067203", 1200),
    ]

    def fetch(spec, max_pages=1):
        return FetchResult(item=item, resolved={}, items=raws, requests=1)

    bot.market.fetch = fetch
    replies: list[str] = []

    async def fake_reply(qq, group_id, message_id, content):
        replies.append(content)

    bot._reply = fake_reply  # type: ignore[assignment]
    return bot, replies


def _set_offline(bot) -> None:
    item = bot.market.resolve("x")
    raw = _raw("898484701286067202", 1050, online=0)
    bot.market.fetch = lambda spec, max_pages=1: FetchResult(
        item=item, resolved={}, items=[raw], requests=1
    )


def test_browse_stores_session_and_caption(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        session = bot._trade_sessions[123]
        assert session.stage == "select"
        assert session.mode == "ask"
        assert len(session.items) == 2
        assert "1. 1,050 卡利弗" in replies[-1]
        assert "2. 1,200 卡利弗" in replies[-1]
        assert "1 私聊购买 / 2 休闲室购买" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_buy_then_choose_method_chat_and_confirm(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert bot._trade_sessions[123].stage == "method"
        assert "请选择购买方式" in replies[-1]
        assert "1. 私聊购买" in replies[-1]
        assert "2. 休闲室购买" in replies[-1]

        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        session = bot._trade_sessions[123]
        assert session.stage == "confirm"
        assert session.mode == "chat"
        assert session.enc == "ENC"
        assert "第 1 步成功（私聊购买）" in replies[-1]
        assert "回复 1 确认发送游戏内私聊" in replies[-1]

        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "私聊购买完成" in replies[-1]
        assert 123 not in bot._trade_sessions
        record = bot.store.list_purchases(123)[0]
        assert record.action == "chat_prepare"
        assert record.mode == "chat"
        assert record.status == "success"
        assert record.enc == "ENC"
    finally:
        bot.store.close()
        bot.market.close()


def test_buy_then_choose_lounge_and_confirm(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 2))
        session = bot._trade_sessions[123]
        assert session.mode == "lounge"
        assert "第 1 步成功（休闲室购买）" in replies[-1]
        assert "确认添加卖家休闲室到游戏中" in replies[-1]
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "休闲室购买完成" in replies[-1]
        record = bot.store.list_purchases(123)[0]
        assert record.action == "lounge_prepare"
        assert record.mode == "lounge"
        assert record.status == "success"
    finally:
        bot.store.close()
        bot.market.close()


def test_preselected_chat_and_lounge_commands(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "求购 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert bot._trade_sessions[123].stage == "confirm"
        assert bot._trade_sessions[123].mode == "chat"
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "私聊购买完成" in replies[-1]

        bot._last_trade[123] = 0
        asyncio.run(bot._handle_command(123, 0, 0, "休闲室 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert bot._trade_sessions[123].stage == "confirm"
        assert bot._trade_sessions[123].mode == "lounge"
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "休闲室购买完成" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_offline_seller_blocks_chat_but_not_lounge(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        _set_offline(bot)
        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "卖家当前离线" in replies[-1]
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "卖家当前不在线" in replies[-1]
        assert bot.store.list_purchases(123) == []
        assert bot._trade_sessions[123].stage == "method"

        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 2))
        assert "休闲室购买" in replies[-1]
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "休闲室购买完成" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_confirm_zero_cancels_record(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "求购 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        pid = bot._trade_sessions[123].record_id
        assert pid
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 0))
        assert "已取消" in replies[-1]
        assert 123 not in bot._trade_sessions
        record = bot.store.get_purchase(pid, 123)
        assert record is not None
        assert record.status == "canceled"
    finally:
        bot.store.close()
        bot.market.close()


def test_trade_error_messages(monkeypatch) -> None:
    bot, replies = _bot(
        monkeypatch,
        chat=TradeResult(step="chat", code="NO_ACCESS_SELLER"),
    )
    try:
        item = bot.market.resolve("x")
        bot.market.fetch = lambda spec, max_pages=1: FetchResult(
            item=item,
            resolved={},
            items=[_raw("898484701286067202", 1050, online=None)],
            requests=1,
        )
        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        assert "卖家当前不在线" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_confirm_command_completes_prepared_record(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "求购 怪物猎人"))
        asyncio.run(bot._handle_trade_choice(123, 0, 0, 1))
        pid = bot._trade_sessions[123].record_id
        assert pid
        asyncio.run(bot._handle_command(123, 0, 0, f"确认 {pid}"))
        assert "私聊购买完成" in replies[-1]
        record = bot.store.get_purchase(pid, 123)
        assert record is not None
        assert record.status == "success"
    finally:
        bot.store.close()
        bot.market.close()


def test_bare_number_selects_item_and_shows_methods(monkeypatch) -> None:
    bot, replies = _bot(monkeypatch)
    try:
        bot.self_id = 999
        asyncio.run(bot._handle_command(123, 0, 0, "购买 怪物猎人"))
        bot._last_command[123] = 0
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123,
            "message_id": 1001,
            "message": [{"type": "text", "data": {"text": "2"}}],
        }
        asyncio.run(bot.handle_event(event))
        session = bot._trade_sessions[123]
        assert session.stage == "method"
        assert session.selected["n8Id"] == "898484701286067203"
        assert "已选 #2" in replies[-1]
        assert "请选择购买方式" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_card_numbering_index_is_preserved() -> None:
    raw = _raw("898484701286067202", 1050)
    raw["data"]["options"] = [
        {"strDesc": "group 0", "n4Point": "10", "n4Group": 0},
        {"strDesc": "group 1", "n4Point": "20", "n4Group": 1},
        {"strDesc": "group 2", "n4Point": "30", "n4Group": 2},
    ]
    listing = _build_listing(raw, set(), "transcendent", 3)
    assert listing.index == 3
