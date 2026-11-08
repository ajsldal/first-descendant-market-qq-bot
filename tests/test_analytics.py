"""订阅抓取分析：消失历史、卖家键、导狗判定与迁移。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from admin.app import create_app
from admin.config import AdminSettings
from market_v2.models import CatalogItem
from qqbot.analytics import AnalyticsRecorder, normalize_transcendent
from qqbot.analytics_migrate import migrate
from qqbot.market import FetchResult
from qqbot.store import Store, Subscription, seller_key

ITEM = CatalogItem(
    item_id="211064002", item_type="transcendent", names={"cn": "怪物猎人"}
)


def _sub(sub_id: int, qq: int = 10001) -> Subscription:
    return Subscription(
        id=sub_id,
        qq=qq,
        spec={"item": {"name": "怪物猎人", "id": "211064002", "type": "transcendent"}},
        fingerprint=f"fp{sub_id}",
        interval_sec=600,
        next_run_at=0,
        enabled=True,
        only_new=False,
        max_push=20,
        quiet_hours="",
        first_push=True,
        fail_count=0,
        last_run_at=None,
        last_count=None,
        last_hash=None,
        created_at=0,
    )


def _raw(
    n8: str,
    *,
    seller: str = "卖家甲#1111",
    perks=(("A", ["a"]), ("B", ["b"])),
    abilities=(10, 20, 30),
    price: float = 600,
    dt: str = "2026-10-27T10:00:00.000",
    quality: int = 400,
) -> dict:
    subperks = [
        {
            "strName": name,
            "isFixed": 0,
            "options": [{"strDesc": d} for d in details],
        }
        for name, details in perks
    ]
    subperks.append({"strName": "固定词条", "isFixed": 1, "options": []})
    options = [
        {"strDesc": f"group {g}", "n4Point": str(point), "n4Group": g}
        for g, point in enumerate(abilities)
    ]
    return {
        "n8Id": n8,
        "n4SalePrice": str(price),
        "dtRegister": dt,
        "seller": {"strNickName": seller},
        "data": {"n4WeaponQuality": quality, "subperks": subperks, "options": options},
    }


def _result(items: list[dict], *, complete: bool = True) -> FetchResult:
    return FetchResult(
        item=ITEM,
        resolved={},
        items=items,
        requests=1,
        total_count=len(items),
        complete=complete,
    )


def _flips(store: Store, item_id: str = ITEM.item_id) -> list[sqlite3.Row]:
    return store._conn.execute(
        "SELECT * FROM seller_flips WHERE item_id = ? ORDER BY id", (item_id,)
    ).fetchall()


def _sellers(store: Store, item_id: str = ITEM.item_id) -> list[sqlite3.Row]:
    return store._conn.execute(
        "SELECT * FROM item_sellers WHERE item_id = ? ORDER BY first_seen_at",
        (item_id,),
    ).fetchall()


# ── 规范化与卖家键 ─────────────────────────────────────────


def test_normalize_drops_fixed_subperk_and_reads_abilities():
    snap = normalize_transcendent(_raw("1"))
    assert snap is not None
    assert [p["name"] for p in snap.subperks] == ["A", "B"]
    assert snap.abilities == {"0": 10, "1": 20, "2": 30}
    assert snap.seller == "卖家甲#1111"
    assert snap.seller_key == "1111"


def test_identity_key_is_order_independent():
    a = normalize_transcendent(_raw("1"))
    b = normalize_transcendent(
        _raw("1", perks=(("B", ["b"]), ("A", ["a"])))
    )
    assert a is not None and b is not None
    assert a.identity_key == b.identity_key
    assert a.identity_key != ""


def test_quality_participates_in_identity():
    old = normalize_transcendent(_raw("1", quality=400))
    new = normalize_transcendent(_raw("1", quality=300))
    assert old is not None and new is not None
    assert old.identity_key != new.identity_key


def test_identity_requires_two_optional_subperks():
    snap = normalize_transcendent(_raw("1", perks=(("A", ["a"]),)))
    assert snap is not None
    assert snap.identity_key == ""


def test_seller_key_uses_number_suffix():
    assert seller_key("Gutala#3157") == "3157"
    assert seller_key("改名后的昵称#3157") == "3157"
    assert seller_key("Gutala # 3157") == "3157"
    assert seller_key("没有编号") == "没有编号"
    assert seller_key("") == ""


# ── 记录流程 ────────────────────────────────────────────────


def test_only_whitelisted_transcendent_is_recorded(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        assert recorder.record_group([_sub(1, 10001)], _result([_raw("1")]), "fp", now=1000) == 1
        assert len(store.list_crawl_runs()) == 1
        assert len(store.list_active_sub_listings(1, ITEM.item_id)) == 1
    finally:
        store.close()

    store = Store(tmp_path / "b.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({99999}))
        assert recorder.record_group([_sub(1, 10001)], _result([_raw("1")]), "fp", now=1000) == 0
        assert store.list_crawl_runs() == []
    finally:
        store.close()

    store = Store(tmp_path / "c.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        ancestor = CatalogItem(item_id="300001", item_type="ancestor", names={"cn": "模块"})
        result = FetchResult(item=ancestor, resolved={}, items=[_raw("1")], requests=1)
        assert recorder.record_group([_sub(1, 10001)], result, "fp", now=1000) == 0
        assert store.list_crawl_runs() == []
    finally:
        store.close()


def test_incomplete_crawl_does_not_mark_disappearance(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("1"), _raw("2")]), "fp", now=1000)
        recorder.record_group([sub], _result([_raw("1")], complete=False), "fp", now=2000)
        history = store._conn.execute(
            "SELECT COUNT(*) FROM disappeared_history WHERE item_id = ?",
            (ITEM.item_id,),
        ).fetchone()[0]
        assert history == 0
        assert len(store.list_active_sub_listings(1, ITEM.item_id)) == 2
    finally:
        store.close()


def test_same_seller_relist_is_not_flip(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100")]), "fp", now=1000)
        recorder.record_group([sub], _result([]), "fp", now=2000)
        recorder.record_group([sub], _result([_raw("300")]), "fp", now=3000)

        sellers = _sellers(store)
        assert len(sellers) == 1
        assert sellers[0]["is_original"] == 1
        assert sellers[0]["listing_count"] == 2
        assert sellers[0]["last_disappeared_at"] == 2000
        assert _flips(store) == []

        row = store.list_sub_listings(1, ITEM.item_id)[-1]
        assert row["state"] == "active"
    finally:
        store.close()


def test_reseller_after_disappearance_is_flip(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group(
            [sub], _result([_raw("100", seller="Gutala#3157", price=6400)]), "fp", now=1000
        )
        recorder.record_group([sub], _result([]), "fp", now=2000)
        recorder.record_group(
            [sub], _result([_raw("300", seller="Gandalion#9951", price=6950)]), "fp", now=3000
        )

        flips = _flips(store)
        assert len(flips) == 1
        flip = flips[0]
        assert flip["original_seller_key"] == "3157"
        assert flip["prev_seller_key"] == "3157"
        assert flip["reseller_key"] == "9951"
        assert flip["source_disappeared_at"] == 2000
        assert flip["reseller_first_seen_at"] == 3000
        assert flip["gap_sec"] == 1000
        assert flip["source_price"] == 6400
        assert flip["reseller_price"] == 6950
        assert flip["verified"] == 1

        sellers = {row["seller_key"]: row for row in _sellers(store)}
        assert sellers["3157"]["is_original"] == 1
        assert sellers["9951"]["is_original"] == 0
        assert sellers["9951"]["flip_in_count"] == 1
    finally:
        store.close()


def test_concurrent_seller_is_not_flip(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100", seller="A#1")]), "fp", now=1000)
        recorder.record_group(
            [sub], _result([_raw("100", seller="A#1"), _raw("300", seller="B#2")]),
            "fp", now=2000,
        )
        recorder.record_group([sub], _result([_raw("300", seller="B#2")]), "fp", now=3000)
        assert _flips(store) == []
        sellers = {row["seller_key"]: row for row in _sellers(store)}
        assert sellers["2"]["flip_in_count"] == 0
    finally:
        store.close()


def test_chain_resale_tracks_previous_owner(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100", seller="A#1", price=600)]), "fp", now=1000)
        recorder.record_group([sub], _result([]), "fp", now=2000)
        recorder.record_group([sub], _result([_raw("300", seller="B#2", price=800)]), "fp", now=3000)
        recorder.record_group([sub], _result([]), "fp", now=4000)
        recorder.record_group([sub], _result([_raw("500", seller="C#3", price=900)]), "fp", now=5000)

        flips = _flips(store)
        assert len(flips) == 2
        assert flips[0]["original_seller_key"] == "1"
        assert flips[0]["prev_seller_key"] == "1"
        assert flips[0]["reseller_key"] == "2"
        assert flips[1]["original_seller_key"] == "1"
        assert flips[1]["prev_seller_key"] == "2"
        assert flips[1]["reseller_key"] == "3"
        assert flips[1]["source_disappeared_at"] == 4000
        assert flips[1]["reseller_first_seen_at"] == 5000
    finally:
        store.close()


def test_rename_keeps_same_seller(tmp_path):
    store = Store(tmp_path / "a.db")
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100", seller="旧名#3157")]), "fp", now=1000)
        recorder.record_group([sub], _result([]), "fp", now=2000)
        recorder.record_group([sub], _result([_raw("300", seller="新名#3157")]), "fp", now=3000)

        assert _flips(store) == []
        sellers = _sellers(store)
        assert len(sellers) == 1
        assert sellers[0]["seller_key"] == "3157"
        assert sellers[0]["seller_name"] == "新名#3157"
    finally:
        store.close()


# ── 迁移回填 ────────────────────────────────────────────────


def test_migration_rebuilds_seller_tables(tmp_path):
    path = tmp_path / "qqbot.db"
    store = Store(path)
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100", seller="A#1", price=600)]), "fp", now=1000)
        recorder.record_group([sub], _result([]), "fp", now=2000)
        recorder.record_group([sub], _result([_raw("300", seller="B#2", price=800)]), "fp", now=3000)
        assert len(_flips(store)) == 1
    finally:
        store.close()

    result = migrate(path)
    assert result["item_sellers"] >= 2
    assert result["seller_flips"] == 1

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sub_listings)")}
        assert "raw_first_json" not in columns
        assert "raw_last_json" not in columns
        assert "seller_key" in columns
        hist_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(disappeared_history)")
        }
        assert "n8_id" not in hist_columns
        assert "raw_json" not in hist_columns
        assert "seller_key" in hist_columns
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "reappear_checks" not in tables
        assert "item_sellers" in tables
        assert "seller_flips" in tables
        flip = conn.execute("SELECT * FROM seller_flips").fetchone()
        assert flip["original_seller_key"] == "1"
        assert flip["reseller_key"] == "2"
        assert flip["verified"] == 1
    finally:
        conn.close()


# ── 后台接口 ────────────────────────────────────────────────


def test_analytics_api_without_n8(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")

    bot_path = tmp_path / "qqbot.db"
    store = Store(bot_path)
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100", seller="A#1", price=600)]), "fp", now=1000)
        recorder.record_group([sub], _result([]), "fp", now=2000)
        recorder.record_group([sub], _result([_raw("300", seller="B#2", price=800)]), "fp", now=3000)
    finally:
        store.close()

    app = create_app(AdminSettings(
        bot_db=bot_path, admin_db=tmp_path / "admin.db", secure_cookies=False
    ))
    client = TestClient(app)
    try:
        client.get("/api/auth/csrf")
        token = client.cookies.get("tfd_admin_csrf") or ""
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "testpass123"},
            headers={"X-CSRF-Token": token},
        ).status_code == 200

        groups = client.get("/api/analytics/identity-groups").json()["items"]
        assert len(groups) == 1
        assert groups[0]["sellers"] == 2
        assert "n8_variants" not in groups[0]

        sellers = client.get(
            "/api/analytics/group-sellers",
            params={"item_id": ITEM.item_id, "identity_key": groups[0]["identity_key"]},
        ).json()["items"]
        assert [row["seller_key"] for row in sellers] == ["1", "2"]
        assert sellers[0]["is_original"] == 1

        flips = client.get("/api/analytics/flips").json()["items"]
        assert len(flips) == 1
        assert flips[0]["original_seller"] == "A#1"
        assert flips[0]["reseller"] == "B#2"

        detail = client.get(
            "/api/analytics/identity-detail",
            params={
                "item_id": ITEM.item_id,
                "identity_key": groups[0]["identity_key"],
            },
        ).json()["item"]
        assert detail["quality"] == 400
        assert detail["abilities"] == {"0": 10, "1": 20, "2": 30}
        assert len(detail["seller_list"]) == 2
        assert detail["sellers"] == 2
        assert len(detail["flips"]) == 1
        assert len(detail["history"]) == 2

        disappeared = client.get("/api/analytics/disappeared").json()["items"]
        assert disappeared and "n8_id" not in disappeared[0]

        assert client.get("/api/analytics/identity").status_code == 404
        assert client.get("/api/analytics/reappear-checks").status_code == 404
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()


def test_identity_groups_sorted_by_listings(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_INITIAL_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "testpass123")

    bot_path = tmp_path / "qqbot.db"
    store = Store(bot_path)
    try:
        recorder = AnalyticsRecorder(store, frozenset({10001}))
        sub = _sub(1)
        recorder.record_group([sub], _result([_raw("100")]), "fp", now=1000)
        recorder.record_group(
            [sub],
            _result([
                _raw("201", perks=(("C", ["c"]), ("D", ["d"])), abilities=(11, 21, 31)),
                _raw("202", perks=(("C", ["c"]), ("D", ["d"])), abilities=(11, 21, 31)),
                _raw("203", perks=(("C", ["c"]), ("D", ["d"])), abilities=(11, 21, 31)),
            ]),
            "fp",
            now=2000,
        )
    finally:
        store.close()

    app = create_app(AdminSettings(
        bot_db=bot_path, admin_db=tmp_path / "admin.db", secure_cookies=False
    ))
    client = TestClient(app)
    try:
        client.get("/api/auth/csrf")
        token = client.cookies.get("tfd_admin_csrf") or ""
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "testpass123"},
            headers={"X-CSRF-Token": token},
        ).status_code == 200

        groups = client.get(
            "/api/analytics/identity-groups", params={"min_sellers": 0}
        ).json()["items"]
        assert [row["listings"] for row in groups] == [3, 1]
        assert all("n8_variants" not in row for row in groups)
    finally:
        app.state.admin_db.close()
        app.state.bot_db.close()
