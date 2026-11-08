"""后台管理 API 基础测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from admin.app import create_app
from admin.config import AdminSettings
from qqbot.store import Store


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")

    bot_path = tmp_path / "qqbot.db"
    store = Store(bot_path)
    store.upsert_user(10001)
    store.add_subscription(
        10001,
        {"item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"}},
        "fp1",
        interval_sec=600,
        max_push=20,
        only_new=False,
        quiet_hours="",
        first_push=True,
        next_run_at=0,
    )
    store.close()

    settings = AdminSettings(
        bot_db=bot_path,
        admin_db=tmp_path / "admin.db",
        secure_cookies=False,
    )
    app = create_app(settings)
    return app, TestClient(app)


def _csrf(client: TestClient) -> str:
    client.get("/api/auth/csrf")
    return client.cookies.get("tfd_admin_csrf") or ""


def _login(client: TestClient) -> str:
    token = _csrf(client)
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass123"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return client.cookies.get("tfd_admin_csrf") or ""


def test_admin_auth_and_permissions(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)
    try:
        # 未登录
        assert client.get("/api/settings").status_code == 401
        # 缺少 CSRF
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": "testpass123"}
        ).status_code == 403
        # 密码错误
        token = _csrf(client)
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
            headers={"X-CSRF-Token": token},
        ).status_code == 401

        token = _login(client)
        assert client.get("/api/auth/me").json()["username"] == "admin"

        settings = client.get("/api/settings").json()["settings"]
        assert settings["analytics_enabled"] is True

        updated = client.put(
            "/api/settings",
            json={
                "analytics_whitelist_qq": [10001],
                "max_subscriptions": 9,
                "login_allowed_qq": [10001],
            },
            headers={"X-CSRF-Token": token},
        )
        assert updated.status_code == 200
        assert updated.json()["settings"]["analytics_whitelist_qq"] == [10001]
        assert updated.json()["settings"]["max_subscriptions"] == 9
        assert updated.json()["settings"]["login_allowed_qq"] == [10001]

        users = client.get("/api/users").json()["items"]
        assert users[0]["qq"] == 10001
        assert users[0]["allow_trade"] == 1

        perm = client.put(
            "/api/users/10001/permissions",
            json={"allow_trade": False, "max_subscriptions": 2},
            headers={"X-CSRF-Token": token},
        )
        assert perm.status_code == 200
        assert perm.json()["permissions"]["allow_trade"] == 0
        assert perm.json()["permissions"]["max_subscriptions"] == 2

        blocked = client.post(
            "/api/users/10001/block",
            json={"reason": "测试封禁"},
            headers={"X-CSRF-Token": token},
        )
        assert blocked.status_code == 200
        assert client.get("/api/users").json()["items"][0]["blocked"] == 1

        assert client.delete(
            "/api/users/10001/block", headers={"X-CSRF-Token": token}
        ).status_code == 200

        assert client.post(
            "/api/subscriptions/1/pause", headers={"X-CSRF-Token": token}
        ).status_code == 200
        assert client.get("/api/subscriptions/1").json()["subscription"]["enabled"] == 0
        assert client.post(
            "/api/subscriptions/1/resume", headers={"X-CSRF-Token": token}
        ).status_code == 200

        audit = client.get("/api/audit").json()["items"]
        actions = {item["action"] for item in audit}
        assert "settings.update" in actions
        assert "user.permissions.update" in actions
        assert "user.block" in actions
        assert "subscription.pause" in actions

        assert client.delete(
            "/api/subscriptions/1", headers={"X-CSRF-Token": token}
        ).status_code == 200
        assert client.get("/api/subscriptions/1").status_code == 404
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()


def test_admin_csrf_required_for_writes(monkeypatch, tmp_path):
    app, client = _client(monkeypatch, tmp_path)
    try:
        token = _login(client)
        assert client.put(
            "/api/settings", json={"max_subscriptions": 5}
        ).status_code == 403
        assert client.put(
            "/api/settings",
            json={"max_subscriptions": 5},
            headers={"X-CSRF-Token": token + "x"},
        ).status_code == 403
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()


def test_steam_credentials_are_write_only(monkeypatch, tmp_path):
    monkeypatch.setenv("TFD_STEAM_VAULT", str(tmp_path / "steam_vault.bin"))
    app, client = _client(monkeypatch, tmp_path)
    try:
        token = _login(client)
        status = client.get("/api/steam/status").json()
        assert status["configured"] is False

        response = client.put(
            "/api/steam/credentials",
            json={"username": "steam-user", "password": "steam-pass"},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200, response.text
        assert response.json()["configured"] is True
        assert "steam-user" not in response.text
        assert "steam-pass" not in response.text

        status = client.get("/api/steam/status").json()
        assert status["configured"] is True
        assert status["readable"] is True
        assert "steam-user" not in str(status)

        audit = client.get("/api/audit").json()["items"]
        actions = {item["action"] for item in audit}
        assert "steam.credentials.update" in actions
        assert all(
            "steam-pass" not in str(item.get("before"))
            and "steam-pass" not in str(item.get("after"))
            for item in audit
        )

        assert client.put(
            "/api/steam/credentials",
            json={"username": "other", "password": "other"},
        ).status_code == 403

        cleared = client.delete(
            "/api/steam/credentials", headers={"X-CSRF-Token": token}
        )
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()
