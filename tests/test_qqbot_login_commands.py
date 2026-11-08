from __future__ import annotations

import asyncio
import json

import pytest
from qqbot import bot as botmod
from qqbot.bot import QQBot, BotSettings
from qqbot.trade import TradeResult

CORE = {"_if_game_auth": "auth", "_ifwt_guest_2392": "wt", "A2SK": "act"}


def _make_bot() -> QQBot:
    return QQBot(
        BotSettings(
            db_path=":memory:",
            token="x",
            login_allowed_qq=frozenset({123}),
        )
    )


def _capture(bot: QQBot) -> list[str]:
    replies: list[str] = []

    async def fake_reply(qq, group_id, message_id, content):
        replies.append(content)

    bot._reply = fake_reply  # type: ignore[assignment]
    return replies


def _usable_session(monkeypatch) -> None:
    def fake_check(cookies, user_agent="", timeout=20.0, transport=None):
        assert cookies["_if_game_auth"] == "auth"
        return TradeResult(step="chat", code="NO_ITEM")

    monkeypatch.setattr(botmod, "check_session", fake_check)


def test_import_login_creates_buffer() -> None:
    bot = _make_bot()
    replies = _capture(bot)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "导入登录"))
        assert 123 in bot._cookie_imports
        assert "Cookie 字符串" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_import_group_is_rejected() -> None:
    bot = _make_bot()
    replies = _capture(bot)
    try:
        asyncio.run(bot._handle_command(123, 999, 0, "导入登录"))
        assert 123 not in bot._cookie_imports
        assert "私聊" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_feed_import_single_message(monkeypatch) -> None:
    _usable_session(monkeypatch)
    bot = _make_bot()
    replies = _capture(bot)
    try:
        bot._cookie_imports[123] = botmod.ImportSession(parts=[], updated_at=0)
        asyncio.run(
            bot._feed_cookie_import(123, 0, 0, json.dumps(CORE))
        )
        assert 123 not in bot._cookie_imports
        token = bot.store.get_token(123)
        assert token is not None
        assert token.method == "cookie_import"
        assert any("导入成功" in r for r in replies)
    finally:
        bot.store.close()
        bot.market.close()


def test_feed_import_multi_message(monkeypatch) -> None:
    _usable_session(monkeypatch)
    bot = _make_bot()
    replies = _capture(bot)
    try:
        bot._cookie_imports[123] = botmod.ImportSession(parts=[], updated_at=0)
        payload = json.dumps(CORE)
        asyncio.run(bot._feed_cookie_import(123, 0, 0, payload[:10]))
        assert 123 in bot._cookie_imports
        assert any("继续粘贴" in r for r in replies)
        asyncio.run(bot._feed_cookie_import(123, 0, 0, payload[10:]))
        assert 123 not in bot._cookie_imports
        assert bot.store.get_token(123) is not None
    finally:
        bot.store.close()
        bot.market.close()


def test_finish_import_without_content() -> None:
    bot = _make_bot()
    replies = _capture(bot)
    try:
        asyncio.run(bot._handle_command(123, 0, 0, "完成导入"))
        assert "没有收到 Cookie" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()


def test_cancel_login_clears_import_buffer() -> None:
    bot = _make_bot()
    replies = _capture(bot)
    try:
        bot._cookie_imports[123] = botmod.ImportSession(parts=["x"], updated_at=0)
        asyncio.run(bot._handle_command(123, 0, 0, "取消登录"))
        assert 123 not in bot._cookie_imports
        assert "已取消" in replies[-1]
    finally:
        bot.store.close()
        bot.market.close()
