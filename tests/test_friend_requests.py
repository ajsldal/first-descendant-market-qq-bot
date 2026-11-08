"""好友申请自动通过与新好友欢迎测试。"""

from __future__ import annotations

import asyncio

from admin.db import BotDB
from qqbot import bot as botmod
from qqbot.bot import BotSettings, QQBot


class FakeClient:
    def __init__(self) -> None:
        self.approvals: list[tuple[str, bool, str]] = []
        self.sent: list[dict] = []
        self.fail_approve = False
        self.friend_ids = {10001}

    def get_friend_list(self):
        return [{"user_id": qq} for qq in self.friend_ids]

    def set_friend_add_request(self, flag, approve=True, remark=""):
        if self.fail_approve:
            raise RuntimeError("approve failed")
        self.approvals.append((flag, bool(approve), remark))
        return {}

    def send_private_msg(self, qq, segments):
        self.sent.append({"qq": qq, "segs": segments})
        return {}

    def send_group_msg(self, group_id, segments):
        self.sent.append({"group": group_id, "segs": segments})
        return {}


def _make_bot(tmp_path, **overrides) -> QQBot:
    bot = QQBot(
        BotSettings(db_path=str(tmp_path / "bot.db"), token="x", **overrides)
    )
    bot.self_id = 3964200143
    bot.client = FakeClient()
    bot._guide_image_segment = lambda: {
        "type": "image",
        "data": {"file": "base64://guide"},
    }
    return bot


def _friend_event(uid: int = 10002, flag: str = "f1", comment: str = "想用机器人") -> dict:
    return {
        "post_type": "request",
        "request_type": "friend",
        "user_id": uid,
        "flag": flag,
        "comment": comment,
    }


def _close(bot: QQBot) -> None:
    bot.scheduler.close()
    bot.market.close()
    bot.store.close()


def test_friend_request_auto_accept_and_welcome(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "FRIEND_WELCOME_DELAY", 0)
    bot = _make_bot(tmp_path)
    try:
        asyncio.run(bot.handle_event(_friend_event()))
        assert bot.client.approvals == [("f1", True, "")]
        assert len(bot.client.sent) == 1
        segments = bot.client.sent[0]["segs"]
        assert segments[0]["type"] == "text"
        assert "已通过你的好友申请" in segments[0]["data"]["text"]
        assert "/订阅" in segments[0]["data"]["text"]
        assert any(seg.get("type") == "image" for seg in segments)
    finally:
        _close(bot)


def test_friend_request_can_be_disabled(tmp_path):
    bot = _make_bot(tmp_path, friend_auto_accept_enabled=False)
    try:
        asyncio.run(bot.handle_event(_friend_event()))
        assert bot.client.approvals == []
        assert bot.client.sent == []
    finally:
        _close(bot)


def test_friend_welcome_can_be_disabled(tmp_path):
    bot = _make_bot(tmp_path, friend_welcome_enabled=False)
    try:
        asyncio.run(bot.handle_event(_friend_event()))
        assert bot.client.approvals == [("f1", True, "")]
        assert bot.client.sent == []
    finally:
        _close(bot)


def test_blocked_user_request_is_ignored(tmp_path):
    bot = _make_bot(tmp_path)
    admin_db = BotDB(tmp_path / "bot.db")
    try:
        admin_db.block_user(10002, reason="test", until=None, admin_id=0)
        bot.permissions.invalidate(10002)
        asyncio.run(bot.handle_event(_friend_event()))
        assert bot.client.approvals == []
        assert bot.client.sent == []
    finally:
        admin_db.close()
        _close(bot)


def test_duplicate_flag_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "FRIEND_WELCOME_DELAY", 0)
    bot = _make_bot(tmp_path)
    try:
        asyncio.run(bot.handle_event(_friend_event()))
        asyncio.run(bot.handle_event(_friend_event()))
        assert len(bot.client.approvals) == 1
        assert len(bot.client.sent) == 1
    finally:
        _close(bot)


def test_friend_accept_rate_limit(tmp_path):
    bot = _make_bot(tmp_path, friend_accept_max_per_min=1)
    try:
        asyncio.run(bot.handle_event(_friend_event(uid=10002, flag="f1")))
        asyncio.run(bot.handle_event(_friend_event(uid=10003, flag="f2")))
        assert len(bot.client.approvals) == 1
    finally:
        _close(bot)


def test_approve_failure_skips_welcome(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "FRIEND_WELCOME_DELAY", 0)
    bot = _make_bot(tmp_path)
    bot.client.fail_approve = True
    try:
        asyncio.run(bot.handle_event(_friend_event()))
        assert bot.client.approvals == []
        assert bot.client.sent == []
    finally:
        _close(bot)


def test_subscription_commands_require_friend(tmp_path):
    bot = _make_bot(tmp_path)
    try:
        asyncio.run(
            bot._handle_command(10002, 0, 0, "订阅 怪物猎人 价格=100-5000")
        )
        text = bot.client.sent[-1]["segs"][-1]["data"]["text"]
        assert "仅对好友开放" in text

        asyncio.run(bot._handle_command(10002, 0, 0, "我的订阅"))
        text = bot.client.sent[-1]["segs"][-1]["data"]["text"]
        assert "仅对好友开放" in text

        # 好友不受影响
        asyncio.run(bot._handle_command(10001, 0, 0, "我的订阅"))
        text = bot.client.sent[-1]["segs"][-1]["data"]["text"]
        assert "还没有订阅" in text

        # 查价等查询命令不要求好友
        asyncio.run(bot._handle_command(10002, 0, 0, "查价"))
        text = bot.client.sent[-1]["segs"][-1]["data"]["text"]
        assert "价格查询用法" in text
    finally:
        _close(bot)
