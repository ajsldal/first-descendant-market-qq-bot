"""成交统计：解析、增量入库与接口测试。"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi.testclient import TestClient

from admin.app import create_app
from admin.config import AdminSettings
from admin.db import AdminDB
from admin.trade_stats import CST, parse_points

PAYLOAD = {
    "IsSuccess": True,
    "ResultCode": "SUCCESS",
    "ResultData": [
        {"date": "2026-10-20 23:00", "average_price": "88", "trade_count": 5},
        {"date": "2026-10-20 23:00", "average_price": "90", "trade_count": 2},
        {"date": "2026-10-20 22:00", "average_price": "120.5", "trade_count": 1},
        {"date": "坏时间", "average_price": "50", "trade_count": 3},
        {"date": "2026-10-20 21:00", "average_price": "0", "trade_count": 3},
        {"date": "2026-10-20 20:00", "average_price": "70", "trade_count": 0},
        {"date": "2026-10-20 19:00", "average_price": None, "trade_count": 2},
    ],
}


def test_parse_points_filters_and_dedupes():
    points = parse_points(PAYLOAD)
    assert len(points) == 2
    assert points[0].bucket_at == int(
        datetime(2026, 10, 20, 22, 0, tzinfo=CST).timestamp()
    )
    assert points[0].average_price == 120.5
    # 同一小时重复时后出现的覆盖前一条
    assert points[1].average_price == 90
    assert points[1].trade_count == 2


def test_parse_points_empty_payload():
    assert parse_points({}) == []
    assert parse_points({"ResultData": None}) == []


def test_insert_points_is_incremental(tmp_path):
    db = AdminDB(tmp_path / "admin.db")
    try:
        points = [(1_700_000_000, 88.0, 5), (1_700_003_600, 90.0, 2)]
        assert db.insert_trade_stats_points("211064002", points) == 2
        assert db.insert_trade_stats_points("211064002", points) == 0
        # 只追加新的时间点
        assert db.insert_trade_stats_points(
            "211064002", points + [(1_700_007_200, 95.0, 1)]
        ) == 1

        db.upsert_trade_stats_item("211064002", "怪物猎人", crawled_at=1_700_000_000)
        rows = db.list_trade_stats_items()
        assert len(rows) == 1
        assert rows[0]["point_count"] == 3
        assert rows[0]["last_bucket_at"] == 1_700_007_200
        assert rows[0]["last_price"] == 95.0

        detail = db.list_trade_stats_points("211064002")
        assert [row["bucket_at"] for row in detail] == [
            1_700_000_000,
            1_700_003_600,
            1_700_007_200,
        ]
    finally:
        db.close()


def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")
    settings = AdminSettings(
        bot_db=tmp_path / "qqbot.db",
        admin_db=tmp_path / "admin.db",
        secure_cookies=False,
    )
    app = create_app(settings)
    return app, TestClient(app)


def _login(client: TestClient) -> str:
    client.get("/api/auth/csrf")
    token = client.cookies.get("tfd_admin_csrf") or ""
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass123"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return client.cookies.get("tfd_admin_csrf") or ""


def test_trade_stats_api(monkeypatch, tmp_path):
    app, client = _app(monkeypatch, tmp_path)
    try:
        assert client.get("/api/trade-stats/items").status_code == 401
        token = _login(client)

        assert client.get("/api/trade-stats/items").json() == {"items": []}
        assert client.get("/api/trade-stats/status").json()["interval_hours"] == 12
        assert client.get("/api/trade-stats/211064002").status_code == 404

        db = app.state.admin_db
        now = int(time.time())
        db.upsert_trade_stats_item("211064002", "怪物猎人", crawled_at=now)
        db.insert_trade_stats_points(
            "211064002",
            [
                (now - 3 * 86400, 80.0, 4),  # 超出 2 天窗口，仍入库但不展示
                (now - 7200, 88.0, 5),
                (now - 3600, 99.0, 3),
            ],
        )

        items = client.get("/api/trade-stats/items").json()["items"]
        assert items[0]["item_id"] == "211064002"
        assert items[0]["point_count"] == 3
        assert items[0]["trades_24h"] == 8

        detail = client.get("/api/trade-stats/211064002").json()["item"]
        # 接口只返回近 2 天窗口
        assert len(detail["points"]) == 2
        assert detail["points"][0]["average_price"] == 88.0
        assert detail["points"][-1]["average_price"] == 99.0
        expected = datetime.fromtimestamp(now - 7200, CST).strftime("%Y-%m-%d %H:%M")
        assert detail["points"][0]["date"] == expected
        assert detail["first_date"] and detail["last_date"]

        # 手动刷新先打桩，避免真实网络请求
        async def fake_run_once():
            return {
                "started_at": 1,
                "finished_at": 2,
                "items": 12,
                "inserted": 3,
                "failed": 0,
                "error": "",
            }

        app.state.trade_stats.run_once = fake_run_once
        assert client.post("/api/trade-stats/refresh").status_code == 403
        refreshed = client.post(
            "/api/trade-stats/refresh", headers={"X-CSRF-Token": token}
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["summary"]["inserted"] == 3

        audit = client.get("/api/audit").json()["items"]
        assert any(item["action"] == "trade_stats.refresh" for item in audit)
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()


def test_trade_stats_settings(monkeypatch, tmp_path):
    app, client = _app(monkeypatch, tmp_path)
    try:
        token = _login(client)
        bad = client.put(
            "/api/settings",
            json={"trade_stats_interval_hours": 0},
            headers={"X-CSRF-Token": token},
        )
        assert bad.status_code == 422

        ok = client.put(
            "/api/settings",
            json={"trade_stats_enabled": False, "trade_stats_interval_hours": 168},
            headers={"X-CSRF-Token": token},
        )
        assert ok.status_code == 200
        settings = ok.json()["settings"]
        assert settings["trade_stats_enabled"] is False
        assert settings["trade_stats_interval_hours"] == 168
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()
