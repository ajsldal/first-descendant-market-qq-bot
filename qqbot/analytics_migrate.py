"""把订阅分析表迁移到「卖家/导狗」模型（2026-11）。

用法::

    python -m qqbot.analytics_migrate --dry-run
    python -m qqbot.analytics_migrate

迁移会先备份 data/qqbot.db 到 data/backup/，然后：

1. 重建 sub_listings / disappeared_history，去掉 n8Id 比对列与 raw 大字段；
2. 删除 reappear_checks 与相关视图；
3. 建立 item_sellers / seller_flips 并回填卖家角色与已验证的倒卖记录；
4. VACUUM 回收空间。

迁移期间请先停止 bot 与 admin 进程。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .store import _SCHEMA, seller_key

DEFAULT_DB = Path("data/qqbot.db")

DROP_VIEWS = (
    "v_disappeared_by_item",
    "v_id_reuse_evidence",
    "v_manual_review",
    "v_relisted_products",
    "v_n8id_identity",
    "v_identity_groups",
    "v_disappeared_identity",
)

SUB_LISTINGS_NEW = """
CREATE TABLE sub_listings_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id          INTEGER NOT NULL,
    qq              INTEGER NOT NULL,
    item_type       TEXT    NOT NULL,
    item_id         TEXT    NOT NULL,
    item_name       TEXT    NOT NULL,
    n8_id           TEXT    NOT NULL,
    identity_key    TEXT    NOT NULL DEFAULT '',
    identity_json   TEXT    NOT NULL DEFAULT '',
    state           TEXT    NOT NULL DEFAULT 'active',
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    disappeared_at  INTEGER,
    first_run_id    INTEGER,
    last_run_id     INTEGER,
    appear_count    INTEGER NOT NULL DEFAULT 1,
    disappear_count INTEGER NOT NULL DEFAULT 0,
    dt_register     TEXT    NOT NULL DEFAULT '',
    price_first     REAL,
    price_last      REAL,
    seller_first    TEXT    NOT NULL DEFAULT '',
    seller_last     TEXT    NOT NULL DEFAULT '',
    seller_key      TEXT    NOT NULL DEFAULT '',
    quality         INTEGER,
    UNIQUE (sub_id, item_id, n8_id)
)
"""

DISAPPEARED_NEW = """
CREATE TABLE disappeared_history_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key       TEXT    NOT NULL,
    item_type      TEXT    NOT NULL,
    item_id        TEXT    NOT NULL,
    item_name      TEXT    NOT NULL,
    sub_id         INTEGER NOT NULL,
    qq             INTEGER NOT NULL,
    identity_key   TEXT    NOT NULL DEFAULT '',
    identity_json  TEXT    NOT NULL DEFAULT '',
    first_seen_at  INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    disappeared_at INTEGER NOT NULL,
    run_id         INTEGER NOT NULL,
    price          REAL,
    seller         TEXT    NOT NULL DEFAULT '',
    seller_key     TEXT    NOT NULL DEFAULT '',
    dt_register    TEXT    NOT NULL DEFAULT '',
    quality        INTEGER,
    spec_json      TEXT    NOT NULL DEFAULT ''
)
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    backup_dir = db_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"qqbot-before-seller-{stamp}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def _drop_objects(conn: sqlite3.Connection) -> None:
    for view in DROP_VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    conn.execute("DROP TABLE IF EXISTS reappear_checks")
    conn.execute("DROP TABLE IF EXISTS item_sellers")
    conn.execute("DROP TABLE IF EXISTS seller_flips")


def _rebuild_sub_listings(conn: sqlite3.Connection) -> int:
    conn.execute("DROP TABLE IF EXISTS sub_listings_new")
    conn.execute(SUB_LISTINGS_NEW)
    cols = _columns(conn, "sub_listings")
    if "seller_key" not in cols:
        conn.execute(
            "ALTER TABLE sub_listings ADD COLUMN seller_key TEXT NOT NULL DEFAULT ''"
        )
    rows = conn.execute("SELECT * FROM sub_listings").fetchall()
    for row in rows:
        key = str(row["seller_key"] or "") or seller_key(str(row["seller_last"] or ""))
        conn.execute(
            "INSERT INTO sub_listings_new ("
            "id, sub_id, qq, item_type, item_id, item_name, n8_id, identity_key, "
            "identity_json, state, first_seen_at, last_seen_at, disappeared_at, "
            "first_run_id, last_run_id, appear_count, disappear_count, dt_register, "
            "price_first, price_last, seller_first, seller_last, seller_key, quality"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["sub_id"], row["qq"], row["item_type"], row["item_id"],
                row["item_name"], row["n8_id"], row["identity_key"], row["identity_json"],
                row["state"], row["first_seen_at"], row["last_seen_at"],
                row["disappeared_at"], row["first_run_id"], row["last_run_id"],
                row["appear_count"], row["disappear_count"], row["dt_register"],
                row["price_first"], row["price_last"], row["seller_first"],
                row["seller_last"], key, row["quality"],
            ),
        )
    conn.execute("DROP TABLE sub_listings")
    conn.execute("ALTER TABLE sub_listings_new RENAME TO sub_listings")
    return len(rows)


def _rebuild_disappeared(conn: sqlite3.Connection) -> int:
    conn.execute("DROP TABLE IF EXISTS disappeared_history_new")
    conn.execute(DISAPPEARED_NEW)
    cols = _columns(conn, "disappeared_history")
    if "seller_key" not in cols:
        conn.execute(
            "ALTER TABLE disappeared_history ADD COLUMN seller_key TEXT NOT NULL DEFAULT ''"
        )
    rows = conn.execute("SELECT * FROM disappeared_history").fetchall()
    for row in rows:
        key = str(row["seller_key"] or "") or seller_key(str(row["seller"] or ""))
        conn.execute(
            "INSERT INTO disappeared_history_new ("
            "id, item_key, item_type, item_id, item_name, sub_id, qq, identity_key, "
            "identity_json, first_seen_at, last_seen_at, disappeared_at, run_id, "
            "price, seller, seller_key, dt_register, quality, spec_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"], row["item_key"], row["item_type"], row["item_id"],
                row["item_name"], row["sub_id"], row["qq"], row["identity_key"],
                row["identity_json"], row["first_seen_at"], row["last_seen_at"],
                row["disappeared_at"], row["run_id"], row["price"], row["seller"],
                key, row["dt_register"], row["quality"], row["spec_json"],
            ),
        )
    conn.execute("DROP TABLE disappeared_history")
    conn.execute("ALTER TABLE disappeared_history_new RENAME TO disappeared_history")
    return len(rows)


def _backfill_sellers(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT item_id, identity_key, item_name, seller_key, seller_last, "
        "first_seen_at, last_seen_at, disappeared_at, price_first, price_last, quality "
        "FROM sub_listings WHERE identity_key <> '' AND seller_key <> ''"
    ).fetchall()
    count = 0
    for row in rows:
        price_min = row["price_first"] if row["price_first"] is not None else row["price_last"]
        price_max = row["price_last"] if row["price_last"] is not None else row["price_first"]
        conn.execute(
            "INSERT INTO item_sellers ("
            "item_id, identity_key, item_name, seller_key, seller_name, is_original, "
            "first_seen_at, last_seen_at, last_disappeared_at, listing_count, "
            "flip_in_count, price_min, price_max, price_last, quality"
            ") VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 1, 0, ?, ?, ?, ?) "
            "ON CONFLICT(item_id, identity_key, seller_key) DO UPDATE SET "
            "seller_name = excluded.seller_name, "
            "first_seen_at = MIN(item_sellers.first_seen_at, excluded.first_seen_at), "
            "last_seen_at = MAX(item_sellers.last_seen_at, excluded.last_seen_at), "
            "last_disappeared_at = NULLIF(MAX("
            "COALESCE(item_sellers.last_disappeared_at, 0), "
            "COALESCE(excluded.last_disappeared_at, 0)), 0), "
            "listing_count = item_sellers.listing_count + 1, "
            "price_min = CASE WHEN item_sellers.price_min IS NULL "
            "OR excluded.price_min < item_sellers.price_min "
            "THEN excluded.price_min ELSE item_sellers.price_min END, "
            "price_max = CASE WHEN item_sellers.price_max IS NULL "
            "OR excluded.price_max > item_sellers.price_max "
            "THEN excluded.price_max ELSE item_sellers.price_max END, "
            "price_last = excluded.price_last, "
            "quality = COALESCE(excluded.quality, item_sellers.quality)",
            (
                row["item_id"], row["identity_key"], row["item_name"],
                row["seller_key"], row["seller_last"], row["first_seen_at"],
                row["last_seen_at"], row["disappeared_at"], price_min, price_max,
                row["price_last"], row["quality"],
            ),
        )
        count += 1
    # 每个身份组最早出现的卖家标记为原始卖家
    conn.execute("UPDATE item_sellers SET is_original = 0")
    for item_id, identity_key, key in conn.execute(
        "SELECT item_id, identity_key, seller_key FROM item_sellers AS s "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM item_sellers AS o WHERE o.item_id = s.item_id "
        "  AND o.identity_key = s.identity_key AND ("
        "    o.first_seen_at < s.first_seen_at OR "
        "    (o.first_seen_at = s.first_seen_at AND o.seller_key < s.seller_key)"
        "  )"
        ")"
    ):
        conn.execute(
            "UPDATE item_sellers SET is_original = 1 "
            "WHERE item_id = ? AND identity_key = ? AND seller_key = ?",
            (item_id, identity_key, key),
        )
    return count


def _insert_flip(
    conn: sqlite3.Connection,
    *,
    listing: sqlite3.Row,
    original: sqlite3.Row,
    prev: sqlite3.Row,
    now: int,
) -> int:
    prev_key = str(prev["seller_key"] or "") or seller_key(str(prev["seller"] or ""))
    cur = conn.execute(
        "INSERT OR IGNORE INTO seller_flips ("
        "item_id, item_name, identity_key, original_seller_key, original_seller, "
        "prev_seller_key, prev_seller, reseller_key, reseller, "
        "source_disappeared_id, source_disappeared_at, reseller_first_seen_at, gap_sec, "
        "source_price, reseller_price, quality, identity_json, reseller_listing_key, "
        "reseller_last_seen_at, reseller_disappeared_at, verified, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            listing["item_id"], listing["item_name"], listing["identity_key"],
            original["seller_key"], original["seller_name"],
            prev_key, str(prev["seller"] or ""),
            listing["seller_key"], listing["seller_last"],
            int(prev["id"]), int(prev["disappeared_at"]),
            int(listing["first_seen_at"]),
            int(listing["first_seen_at"]) - int(prev["disappeared_at"]),
            prev["price"], listing["price_last"], listing["quality"],
            listing["identity_json"], listing["n8_id"],
            listing["last_seen_at"], listing["disappeared_at"], now,
        ),
    )
    return int(cur.rowcount)


def _backfill_flips(conn: sqlite3.Connection, *, dry_run: bool = False) -> int:
    now = int(time.time())
    originals = {
        (str(row["item_id"]), str(row["identity_key"])): row
        for row in conn.execute("SELECT * FROM item_sellers WHERE is_original = 1")
    }
    listings = conn.execute(
        "SELECT * FROM sub_listings WHERE identity_key <> '' AND seller_key <> '' "
        "ORDER BY item_id, identity_key, first_seen_at, id"
    ).fetchall()
    inserted = 0
    for listing in listings:
        group = (str(listing["item_id"]), str(listing["identity_key"]))
        original = originals.get(group)
        if original is None or original["seller_key"] == listing["seller_key"]:
            continue
        if conn.execute(
            "SELECT 1 FROM seller_flips WHERE identity_key = ? AND reseller_listing_key = ?",
            (listing["identity_key"], listing["n8_id"]),
        ).fetchone():
            continue
        original_last = conn.execute(
            "SELECT * FROM disappeared_history "
            "WHERE item_key = ? AND identity_key = ? AND seller_key = ? "
            "AND disappeared_at <= ? ORDER BY disappeared_at DESC, id DESC LIMIT 1",
            (
                listing["item_id"], listing["identity_key"],
                original["seller_key"], listing["first_seen_at"],
            ),
        ).fetchone()
        if original_last is None:
            continue
        prev = conn.execute(
            "SELECT * FROM disappeared_history "
            "WHERE item_key = ? AND identity_key = ? AND disappeared_at <= ? "
            "AND seller_key <> ? ORDER BY disappeared_at DESC, id DESC LIMIT 1",
            (
                listing["item_id"], listing["identity_key"],
                listing["first_seen_at"], listing["seller_key"],
            ),
        ).fetchone()
        if prev is None or int(listing["first_seen_at"]) <= int(prev["disappeared_at"]):
            continue
        if dry_run:
            inserted += 1
            continue
        inserted += _insert_flip(
            conn, listing=listing, original=original, prev=prev, now=now
        )
    if not dry_run:
        conn.execute(
            "UPDATE item_sellers SET flip_in_count = ("
            "SELECT COUNT(*) FROM seller_flips AS f WHERE f.identity_key = item_sellers.identity_key "
            "AND f.reseller_key = item_sellers.seller_key)"
        )
    return inserted


def _summarize(conn: sqlite3.Connection) -> dict[str, Any]:
    def count(table: str) -> int:
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.OperationalError:
            return 0

    return {
        "sub_listings": count("sub_listings"),
        "disappeared_history": count("disappeared_history"),
        "item_sellers": count("item_sellers"),
        "seller_flips": count("seller_flips"),
        "reappear_checks": count("reappear_checks"),
    }


def migrate(db_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        if dry_run:
            summary = _summarize(conn)
            has_sellers = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_sellers'"
            ).fetchone()
            if has_sellers and "seller_key" in _columns(conn, "sub_listings"):
                summary["pending_flips"] = _backfill_flips(conn, dry_run=True)
            return summary

        backup = _backup(conn, db_path)
        _drop_objects(conn)
        rebuilt_sub = _rebuild_sub_listings(conn)
        rebuilt_hist = _rebuild_disappeared(conn)
        conn.executescript(_SCHEMA)
        sellers = _backfill_sellers(conn)
        flips = _backfill_flips(conn)
        conn.execute("VACUUM")
        return {
            "backup": str(backup),
            "sub_listings": rebuilt_sub,
            "disappeared_history": rebuilt_hist,
            "item_sellers": sellers,
            "seller_flips": flips,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移订阅分析表到卖家/导狗模型")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"数据库不存在：{path}")
        return 2
    result = migrate(path, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
