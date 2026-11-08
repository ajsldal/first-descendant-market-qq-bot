"""倒卖利润：模糊表构建、利润公式、调度触发与后台接口。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from admin.app import create_app
from admin.config import AdminSettings
from qqbot.dsl import DslError, parse_dsl
from qqbot.flip import (
    build_fuzzy_spec,
    format_profit_message,
    overrides_to_text,
    parse_flip_overrides,
    price_of,
    profit_of,
    select_ability,
)
from qqbot.market import FetchResult
from qqbot.permissions import PermissionService
from qqbot.scheduler import Scheduler
from qqbot.store import Store

QQ = 10001
SPEC = {
    "item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"},
    "price": {"min": 100, "max": 5000},
    "quality": {"min": 390, "max": 400},
    "subperks": ["强化大容量弹夹"],
    "ability1": {"min": 20, "max": 100},
    "ability2": {"min": 143, "max": 150},
    "ability3": {"min": 90, "max": 110},
}


def _item(n8: str, price: float) -> dict:
    return {
        "n8Id": n8,
        "n4SalePrice": price,
        "seller": {"strNickName": f"seller{n8[-2:]}"},
        "data": {
            "n4WeaponQuality": 400,
            "subperks": [
                {"strName": "强化大容量弹夹"},
                {"strName": "巨魔强化弹头"},
            ],
            "options": [
                {"n4Point": "143", "n4Group": 0},
                {"n4Point": "150", "n4Group": 1},
                {"n4Point": "35", "n4Group": 2},
            ],
        },
    }


def test_select_ability_prefers_lower_bound_max() -> None:
    key, rng = select_ability(SPEC)
    assert key == "ability2"
    assert rng == {"min": 143, "max": 150}


def test_select_ability_ignores_empty() -> None:
    spec = {"ability1": {"min": 40, "max": 60}, "ability2": {"min": None, "max": None}}
    assert select_ability(spec) == ("ability1", {"min": 40, "max": 60})
    assert select_ability({}) is None


def test_build_fuzzy_spec_defaults() -> None:
    fuzzy = build_fuzzy_spec(SPEC)
    assert fuzzy["item"] == {"name": "怪物猎人", "id": "211064002", "type": "transcendent"}
    assert fuzzy["quality"] == {"min": 390, "max": 400}
    assert fuzzy["ability2"] == {"min": 143, "max": 150}
    assert "ability1" not in fuzzy and "ability3" not in fuzzy
    assert "price" not in fuzzy and "subperks" not in fuzzy
    assert fuzzy["sort"] == "price_asc"


def test_build_fuzzy_spec_overrides() -> None:
    overrides = parse_flip_overrides("价格=100-8000 辅助=强化大容量弹夹 能力3=90-110")
    fuzzy = build_fuzzy_spec(SPEC, overrides)
    assert fuzzy["price"] == {"min": 100, "max": 8000}
    assert fuzzy["subperks"] == ["强化大容量弹夹"]
    assert fuzzy["ability3"] == {"min": 90, "max": 110}
    assert fuzzy["ability2"] == {"min": 143, "max": 150}


def test_parse_flip_overrides_invalid() -> None:
    with pytest.raises(DslError):
        parse_flip_overrides("价格=abc")


def test_overrides_to_text_roundtrip() -> None:
    overrides = parse_flip_overrides("价格=100-8000 能力2=143-150")
    text = overrides_to_text(overrides)
    assert "价格=100-8000" in text
    assert "能力2=143-150" in text
    assert parse_flip_overrides(text) == overrides


def test_profit_formula() -> None:
    # 卖出 1680，买入 950，手续费 500，税 10%
    assert profit_of(950, 1680, fee=500, tax_rate=0.1) == pytest.approx(62)


def test_format_profit_message_marks_loss() -> None:
    message = format_profit_message(
        sub_id=3, matched=1, cheapest=_item("1", 950), target=_item("2", 1000),
        target_rank=2, profit=-408, fee=500, tax_rate=0.1,
        command="/购买 怪物猎人 价格=950-950",
    )
    assert "亏损不建议" in message
    assert "订阅 #3 · 命中 1 条" in message
    assert "市场第 2 低价" in message
    assert "/购买 怪物猎人" in message


def test_min_interval_is_one_minute() -> None:
    assert parse_dsl("怪物猎人 间隔=1m").options.interval_sec == 60
    with pytest.raises(DslError):
        parse_dsl("怪物猎人 间隔=30s")


class _Pusher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_private(self, qq: int, content: str) -> None:
        self.messages.append(content)


class _Market:
    def fetch(self, spec, max_pages=3):  # noqa: ANN001
        raise AssertionError("不应直接调用市场")


def _scheduler(tmp_path):
    store = Store(tmp_path / "bot.db")
    permissions = PermissionService(store, defaults={"flip_cooldown_sec": 10})
    pusher = _Pusher()
    scheduler = Scheduler(store, _Market(), pusher, permissions=permissions)
    return store, scheduler, pusher


def _sub(store: Store, **fields) -> int:
    sub_id = store.add_subscription(
        QQ, SPEC, "fp1", interval_sec=60, max_push=20,
        only_new=False, quiet_hours="", first_push=True,
    )
    if fields:
        store.update_options(sub_id, QQ, fields)
    return sub_id


def test_scheduler_pushes_profit(tmp_path, monkeypatch):
    store, scheduler, pusher = _scheduler(tmp_path)
    calls: list = []

    async def fake_fetch(fingerprint, spec, *, force=False):
        calls.append(force)
        return FetchResult(
            item=None,  # type: ignore[arg-type]
            resolved={},
            items=[_item("900000000000000001", 1000), _item("900000000000000002", 1680)],
            requests=1,
            complete=True,
        )

    monkeypatch.setattr(scheduler, "_fetch", fake_fetch)
    try:
        sub_id = _sub(store, bark_enabled=1)
        sub = store.get_subscription(sub_id, QQ)
        asyncio.run(scheduler._flip_profit(sub, [_item("900000000000000009", 950)], 1))
        assert calls == [True]
        assert len(pusher.messages) == 1
        assert "最高倒卖利润 ¥62" in pusher.messages[0]

        # 冷却过后同一签名仍然去重
        signature, _ = scheduler._flip_last[sub_id]
        scheduler._flip_last[sub_id] = (signature, time.monotonic() - 3600)
        asyncio.run(scheduler._flip_profit(sub, [_item("900000000000000009", 950)], 1))
        assert len(pusher.messages) == 1
    finally:
        scheduler.close()
        store.close()


def test_scheduler_skips_when_not_bark_or_too_many(tmp_path, monkeypatch):
    store, scheduler, pusher = _scheduler(tmp_path)
    calls: list = []

    async def fake_fetch(fingerprint, spec, *, force=False):
        calls.append(force)
        return FetchResult(item=None, resolved={}, items=[], requests=1, complete=True)  # type: ignore[arg-type]

    monkeypatch.setattr(scheduler, "_fetch", fake_fetch)
    try:
        sub_id = _sub(store)
        sub = store.get_subscription(sub_id, QQ)
        asyncio.run(scheduler._flip_profit(sub, [_item("1", 950)], 1))  # 未开 Bark
        assert calls == []

        store.update_options(sub_id, QQ, {"bark_enabled": 1})
        sub = store.get_subscription(sub_id, QQ)
        asyncio.run(scheduler._flip_profit(sub, [_item(str(i), 900 + i) for i in range(4)], 4))
        assert calls == []
        assert pusher.messages == []
    finally:
        scheduler.close()
        store.close()


def test_scheduler_uses_ability_override(tmp_path, monkeypatch):
    store, scheduler, pusher = _scheduler(tmp_path)
    captured: list[dict] = []

    async def fake_fetch(fingerprint, spec, *, force=False):
        captured.append(spec.to_dict() if hasattr(spec, "to_dict") else spec)
        return FetchResult(
            item=None,  # type: ignore[arg-type]
            resolved={},
            items=[_item("900000000000000001", 1000), _item("900000000000000002", 2000)],
            requests=1,
            complete=True,
        )

    monkeypatch.setattr(scheduler, "_fetch", fake_fetch)
    try:
        sub_id = _sub(
            store, bark_enabled=1,
            flip_overrides_json=json.dumps({"price": {"min": 100, "max": 3000}}),
        )
        sub = store.get_subscription(sub_id, QQ)
        asyncio.run(scheduler._flip_profit(sub, [_item("900000000000000009", 950)], 1))
        assert sub.flip_overrides == {"price": {"min": 100, "max": 3000}}
        assert pusher.messages and "最高倒卖利润" in pusher.messages[0]
        assert price_of(_item("x", 3)) == 3.0
    finally:
        scheduler.close()
        store.close()


def test_admin_subscription_flip_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")
    bot_path = tmp_path / "qqbot.db"
    store = Store(bot_path)
    store.upsert_user(QQ)
    sub_id = store.add_subscription(
        QQ, SPEC, "fp1", interval_sec=60, max_push=20,
        only_new=False, quiet_hours="", first_push=True,
    )
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

    bad = client.patch(
        f"/api/subscriptions/{sub_id}", json={"flip_overrides": "价格=abc"}, headers=headers
    )
    assert bad.status_code == 400

    ok = client.patch(
        f"/api/subscriptions/{sub_id}",
        json={"flip_overrides": "价格=100-3000 能力2=143-150"},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    row = ok.json()["subscription"]
    assert "价格=100-3000" in row["flip_overrides_text"]

    listed = client.get("/api/subscriptions", params={"qq": QQ}).json()["items"]
    assert listed and "能力2=143-150" in listed[0]["flip_overrides_text"]


def test_admin_settings_accepts_flip_keys(monkeypatch, tmp_path):
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
    response = client.put(
        "/api/settings",
        json={
            "flip_enabled": True, "flip_fee": 800,
            "flip_tax_rate": 0.12, "flip_cooldown_sec": 300,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    settings = response.json()["settings"]
    assert settings["flip_fee"] == 800
    assert settings["flip_tax_rate"] == pytest.approx(0.12)
    assert settings["flip_cooldown_sec"] == 300
