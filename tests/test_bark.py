"""Bark 手机推送：解析/模板/存储/节流/命令/后台接口。"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from admin.app import create_app
from admin.config import AdminSettings
from qqbot import bark as barkmod
from qqbot import bot as botmod
from qqbot.bark import (
    BarkService,
    build_command,
    build_payload,
    normalize_sound,
    parse_bark_target,
    protect_key,
)
from qqbot.bot import BotSettings, QQBot
from qqbot.permissions import PermissionService
from qqbot.store import Store

QQ = 10001
ITEM = {
    "n8Id": "898484701287498754",
    "n4SalePrice": "950",
    "data": {
        "n4WeaponQuality": 400,
        "subperks": [
            {"strName": "强化大容量弹夹"},
            {"strName": "巨魔强化弹头 I"},
        ],
        "options": [
            {"n4Point": "101", "n4Group": 0},
            {"n4Point": "102", "n4Group": 1},
            {"n4Point": "103", "n4Group": 2},
        ],
    },
}
SPEC = {
    "item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"},
    "price": {"min": 100, "max": 5000},
    "quality": {"min": 390, "max": 400},
    "subperks": ["强化大容量弹夹"],
}


def test_parse_bark_target() -> None:
    assert parse_bark_target("https://api.day.app/abcdefgh12345678") == (
        "https://api.day.app",
        "abcdefgh12345678",
    )
    assert parse_bark_target("https://bark.example.com/abcdefgh12345678/") == (
        "https://bark.example.com",
        "abcdefgh12345678",
    )
    assert parse_bark_target("abcdefgh12345678") == (
        "https://api.day.app",
        "abcdefgh12345678",
    )
    with pytest.raises(barkmod.BarkError):
        parse_bark_target("https://api.day.app/短")


def test_normalize_sound() -> None:
    assert normalize_sound("") == "alarm"
    assert normalize_sound("alarm") == "alarm"
    assert normalize_sound("minuet") == "minuet"
    with pytest.raises(barkmod.BarkError):
        normalize_sound("bad name")


def test_build_command_exact_values() -> None:
    command = build_command(ITEM, SPEC)
    assert command.startswith("/购买 怪物猎人 类型=超越武器")
    assert "品质=400-400" in command
    assert "价格=950-950" in command
    assert "辅助=强化大容量弹夹" in command
    assert "能力1=101-101" in command
    assert "能力2=102-102" in command
    assert "能力3=103-103" in command
    assert len(command) <= barkmod.BODY_LIMIT


def test_build_command_drops_values_with_spaces() -> None:
    command = build_command(ITEM, {"item": {"name": "怪物猎人", "type": "transcendent"}})
    assert "巨魔强化弹头" not in command
    assert "强化大容量弹夹" in command


def test_build_payload_fields() -> None:
    payload = build_payload(
        sub_id=3,
        matched=9,
        new_count=2,
        command="/购买 怪物猎人 品质=400-400",
        item_name="怪物猎人",
        price="950",
        sound="alarm.caf",
        group="TFD市场",
        level="timeSensitive",
        call=False,
    )
    assert payload["title"] == "怪物猎人 命中 9 条"
    assert payload["subtitle"] == "订阅 #3 · 新 2 条 · 最低 ¥950"
    assert payload["body"] == payload["copy"] == "/购买 怪物猎人 品质=400-400"
    assert payload["sound"] == "alarm.caf"
    assert payload["id"] == "sub-3"
    assert "call" not in payload


def test_post_bark_falls_back_to_curl(monkeypatch):
    monkeypatch.setattr(
        barkmod, "_post_urllib", lambda url, body: (False, "网络/TLS 异常：x", True)
    )
    monkeypatch.setattr(barkmod, "_post_curl", lambda url, body: (True, "", False))
    assert barkmod.post_bark(
        "https://api.day.app", "abcdefgh12345678", {"title": "t"}
    ) == (True, "")


def test_post_bark_reports_error_when_both_fail(monkeypatch):
    monkeypatch.setattr(
        barkmod,
        "_post_urllib",
        lambda url, body: (False, "网络/TLS 异常：<urlopen error [BIO] system lib>", True),
    )
    monkeypatch.setattr(
        barkmod, "_post_curl", lambda url, body: (False, "网络/TLS 异常：timeout", True)
    )
    ok, error = barkmod.post_bark(
        "https://api.day.app", "abcdefgh12345678", {"title": "t"}
    )
    assert ok is False
    assert "网络/TLS" in error


def test_post_bark_does_not_retry_on_http_400(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        barkmod, "_post_urllib", lambda url, body: (False, "HTTP 400 Bad Request", False)
    )
    monkeypatch.setattr(
        barkmod, "_post_curl", lambda url, body: calls.append(1) or (True, "", False)
    )
    ok, error = barkmod.post_bark(
        "https://api.day.app", "abcdefgh12345678", {"title": "t"}
    )
    assert ok is False and "400" in error
    assert calls == []


def test_store_bark_roundtrip(tmp_path) -> None:
    store = Store(tmp_path / "bot.db")
    try:
        key = protect_key("abcdefgh12345678")
        store.save_user_bark(
            QQ, server="https://api.day.app", device_key_enc=key, sound="alarm.caf"
        )
        user = store.get_user_bark(QQ)
        assert user is not None and user.enabled and user.sound == "alarm.caf"
        assert store.set_user_bark_sound(QQ, "minuet.caf")
        assert store.set_user_bark_enabled(QQ, False)
        assert store.get_user_bark(QQ).enabled is False

        sub_id = store.add_subscription(
            QQ,
            SPEC,
            "fp1",
            interval_sec=600,
            max_push=20,
            only_new=False,
            quiet_hours="",
            first_push=True,
            bark_sound="chime.caf",
        )
        assert store.update_options(sub_id, QQ, {"bark_enabled": 1})
        store.mark_bark_sent(sub_id, QQ, 1234)
        sub = store.get_subscription(sub_id, QQ)
        assert sub is not None
        assert sub.bark_enabled and sub.bark_sound == "chime.caf" and sub.bark_last_at == 1234
        assert store.get_user_bark(QQ).last_sent_at == 1234

        assert store.delete_user_bark(QQ)
        assert store.get_user_bark(QQ) is None
    finally:
        store.close()


def test_store_adds_bark_columns_to_old_schema(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qq INTEGER NOT NULL,
            spec_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            interval_sec INTEGER NOT NULL DEFAULT 600,
            next_run_at INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            only_new INTEGER NOT NULL DEFAULT 0,
            max_push INTEGER NOT NULL DEFAULT 20,
            quiet_hours TEXT NOT NULL DEFAULT '',
            first_push INTEGER NOT NULL DEFAULT 1,
            fail_count INTEGER NOT NULL DEFAULT 0,
            last_run_at INTEGER,
            last_count INTEGER,
            last_hash TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        columns = {
            row[1] for row in store._conn.execute("PRAGMA table_info(subscriptions)")
        }
        assert {"bark_enabled", "bark_sound", "bark_last_at"} <= columns
    finally:
        store.close()


def _service(tmp_path, **defaults):
    store = Store(tmp_path / "bot.db")
    permissions = PermissionService(store, defaults=defaults or None)
    service = BarkService(store, permissions)
    service.begin_tick()
    return store, service


def test_service_skips_without_config(tmp_path, monkeypatch):
    store, service = _service(tmp_path)
    calls: list = []
    monkeypatch.setattr(barkmod, "post_bark", lambda *a, **k: calls.append(a) or (True, ""))
    try:
        sub_id = store.add_subscription(
            QQ, SPEC, "fp1", interval_sec=600, max_push=20,
            only_new=False, quiet_hours="", first_push=True,
        )
        store.update_options(sub_id, QQ, {"bark_enabled": 1})
        sub = store.get_subscription(sub_id, QQ)
        asyncio.run(service.notify(sub, [ITEM], 1, 1))
        assert calls == []
    finally:
        store.close()


def test_service_sends_once_and_respects_cooldown(tmp_path, monkeypatch):
    store, service = _service(tmp_path)
    sent: list[dict] = []
    monkeypatch.setattr(
        barkmod, "post_bark", lambda server, key, payload: sent.append(payload) or (True, "")
    )
    try:
        store.save_user_bark(QQ, server="https://api.day.app", device_key_enc=protect_key("abcdefgh12345678"))
        sub_id = store.add_subscription(
            QQ, SPEC, "fp1", interval_sec=600, max_push=20,
            only_new=False, quiet_hours="", first_push=True, bark_sound="chime.caf",
        )
        store.update_options(sub_id, QQ, {"bark_enabled": 1})
        sub = store.get_subscription(sub_id, QQ)

        assert asyncio.run(service.notify(sub, [ITEM], 1, 1)) is True
        assert len(sent) == 1
        assert sent[0]["sound"] == "chime.caf"
        refreshed = store.get_subscription(sub_id, QQ)
        assert refreshed.bark_last_at > 0

        # 冷却期内不再发送
        assert asyncio.run(service.notify(sub, [ITEM], 1, 1)) is False
        assert len(sent) == 1
    finally:
        store.close()


def test_service_rate_limit(tmp_path, monkeypatch):
    store, service = _service(tmp_path, bark_cooldown_sec=10, bark_max_per_min=1)
    sent: list = []
    monkeypatch.setattr(
        barkmod, "post_bark", lambda *a, **k: sent.append(1) or (True, "")
    )
    try:
        store.save_user_bark(QQ, server="https://api.day.app", device_key_enc=protect_key("abcdefgh12345678"))
        sub_id = store.add_subscription(
            QQ, SPEC, "fp1", interval_sec=600, max_push=20,
            only_new=False, quiet_hours="", first_push=True,
        )
        store.update_options(sub_id, QQ, {"bark_enabled": 1})
        sub = store.get_subscription(sub_id, QQ)
        assert asyncio.run(service.notify(sub, [ITEM], 1, 1)) is True
        # 清掉冷却时间，只让每分钟上限生效
        with store._lock, store._conn:
            store._conn.execute("UPDATE subscriptions SET bark_last_at = 0 WHERE id = ?", (sub_id,))
            store._conn.execute("UPDATE user_bark SET last_sent_at = 0 WHERE qq = ?", (QQ,))
        assert asyncio.run(service.notify(sub, [ITEM], 1, 1)) is False
        assert len(sent) == 1
    finally:
        store.close()


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_private_msg(self, qq, segments):
        self.calls.append({"qq": qq, "segments": segments})
        return {}

    def send_group_msg(self, group_id, segments):
        self.calls.append({"group": group_id, "segments": segments})
        return {}


def _bot(tmp_path) -> QQBot:
    bot = QQBot(BotSettings(db_path=str(tmp_path / "bot.db"), token="x", image_enabled=False))
    bot.self_id = 3964200143
    bot.client = FakeClient()
    return bot


def test_bot_bark_setup_private_only(tmp_path):
    bot = _bot(tmp_path)
    try:
        asyncio.run(bot._bark(QQ, 999, 0, "设置 abcdefgh12345678"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "私聊" in text
    finally:
        bot.scheduler.close()
        bot.store.close()


def test_bot_bark_setup_and_status(tmp_path):
    bot = _bot(tmp_path)
    try:
        asyncio.run(bot._bark(QQ, 0, 0, "设置 https://api.day.app/abcdefgh12345678"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "****5678" in text and "撤回" in text
        assert bot.store.get_user_bark(QQ) is not None

        asyncio.run(bot._bark(QQ, 0, 0, ""))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "Bark 手机推送" in text and "默认铃声：alarm" in text
    finally:
        bot.scheduler.close()
        bot.store.close()


def test_bot_bark_subscription_toggle(tmp_path):
    bot = _bot(tmp_path)
    try:
        sub_id = bot.store.add_subscription(
            QQ, SPEC, "fp1", interval_sec=600, max_push=20,
            only_new=False, quiet_hours="", first_push=True,
        )
        asyncio.run(bot._bark(QQ, 0, 0, f"订阅 {sub_id} 开"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "已开启" in text
        assert bot.store.get_subscription(sub_id, QQ).bark_enabled is True
    finally:
        bot.scheduler.close()
        bot.store.close()


def test_bot_bark_test_push(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    sent: list = []
    monkeypatch.setattr(botmod, "post_bark", lambda *a, **k: sent.append(1) or (True, ""))
    try:
        asyncio.run(bot._bark(QQ, 0, 0, "设置 abcdefgh12345678"))
        asyncio.run(bot._bark(QQ, 0, 0, "测试"))
        text = bot.client.calls[-1]["segments"][-1]["data"]["text"]
        assert "测试推送已发送" in text
        assert sent == [1]
    finally:
        bot.scheduler.close()
        bot.store.close()


def test_dsl_sound_option() -> None:
    from qqbot.dsl import parse_dsl, parse_settings

    parsed = parse_dsl("怪物猎人 价格=100-5000 铃声=alarm")
    assert parsed.options.bark_sound == "alarm"
    assert parse_settings("铃声=minuet.caf")["bark_sound"] == "minuet.caf"


def test_admin_bark_api(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")
    monkeypatch.setattr("admin.routers.bark.post_bark", lambda *a, **k: (True, ""))

    bot_path = tmp_path / "qqbot.db"
    store = Store(bot_path)
    store.upsert_user(QQ)
    store.close()

    app = create_app(
        AdminSettings(bot_db=bot_path, admin_db=tmp_path / "admin.db", secure_cookies=False)
    )
    client = TestClient(app)
    client.get("/api/auth/csrf")
    csrf = client.cookies.get("tfd_admin_csrf") or ""
    assert client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass123"},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    csrf = client.cookies.get("tfd_admin_csrf") or ""
    headers = {"X-CSRF-Token": csrf}

    assert client.get(f"/api/users/{QQ}/bark").json()["configured"] is False

    saved = client.put(
        f"/api/users/{QQ}/bark",
        json={"target": "https://api.day.app/abcdefgh12345678", "sound": "alarm"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True
    assert body["key_tail"] == "****5678"
    assert body["sound"] == "alarm"

    assert client.post(
        f"/api/users/{QQ}/bark/test", headers=headers
    ).status_code == 200

    toggled = client.post(
        f"/api/users/{QQ}/bark/toggle", json={"enabled": False}, headers=headers
    )
    assert toggled.json()["enabled"] is False

    assert client.delete(f"/api/users/{QQ}/bark", headers=headers).json()["deleted"] is True
    assert client.get(f"/api/users/{QQ}/bark").json()["configured"] is False


def test_admin_bark_flood_test_sets_call(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_post(server, key, payload):
        captured.update(payload)
        return True, ""

    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")
    monkeypatch.setattr("admin.routers.bark.post_bark", fake_post)

    bot_path = tmp_path / "qqbot.db"
    store = Store(bot_path)
    store.upsert_user(QQ)
    store.close()

    app = create_app(
        AdminSettings(bot_db=bot_path, admin_db=tmp_path / "admin.db", secure_cookies=False)
    )
    client = TestClient(app)
    client.get("/api/auth/csrf")
    csrf = client.cookies.get("tfd_admin_csrf") or ""
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass123"},
        headers={"X-CSRF-Token": csrf},
    )
    csrf = client.cookies.get("tfd_admin_csrf") or ""
    headers = {"X-CSRF-Token": csrf}

    client.put(
        f"/api/users/{QQ}/bark",
        json={"target": "https://api.day.app/abcdefgh12345678", "sound": "alarm"},
        headers=headers,
    )

    normal = client.post(
        f"/api/users/{QQ}/bark/test", params={"mode": "normal"}, headers=headers
    )
    assert normal.status_code == 200
    assert "call" not in captured

    flood = client.post(
        f"/api/users/{QQ}/bark/test", params={"mode": "call"}, headers=headers
    )
    assert flood.status_code == 200
    assert captured["call"] == "1"
    assert captured["level"] == "timeSensitive"
    assert "循环轰炸" in captured["title"]


def test_admin_settings_accepts_bark_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")
    bot_path = tmp_path / "qqbot.db"
    Store(bot_path).close()
    app = create_app(
        AdminSettings(bot_db=bot_path, admin_db=tmp_path / "admin.db", secure_cookies=False)
    )
    client = TestClient(app)
    client.get("/api/auth/csrf")
    csrf = client.cookies.get("tfd_admin_csrf") or ""
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass123"},
        headers={"X-CSRF-Token": csrf},
    )
    csrf = client.cookies.get("tfd_admin_csrf") or ""
    updated = client.put(
        "/api/settings",
        json={
            "bark_enabled": False,
            "bark_cooldown_sec": 300,
            "bark_level": "active",
            "bark_group": "TFD",
            "bark_call": True,
            "bark_max_per_min": 2,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    settings = updated.json()["settings"]
    assert settings["bark_cooldown_sec"] == 300
    assert settings["bark_level"] == "active"
    assert settings["bark_call"] is True
