"""SQLite 存储层：用户、订阅、已见商品、运行记录。

所有操作都在同一把锁内完成（sqlite3 连接跨线程复用），
写入量很小，单连接足够，不需要 ORM 或独立数据库服务。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SELLER_TAG = re.compile(r"#\s*(\d+)\s*$")


def seller_key(name: str) -> str:
    """卖家唯一键：取昵称末尾的 #数字（改名不影响），无编号时回退完整昵称。"""
    text = str(name or "").strip()
    match = _SELLER_TAG.search(text)
    return match.group(1) if match else text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    qq         INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    qq           INTEGER NOT NULL,
    spec_json    TEXT    NOT NULL,
    fingerprint  TEXT    NOT NULL,
    interval_sec INTEGER NOT NULL DEFAULT 600,
    next_run_at  INTEGER NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    only_new     INTEGER NOT NULL DEFAULT 0,
    max_push     INTEGER NOT NULL DEFAULT 20,
    quiet_hours  TEXT    NOT NULL DEFAULT '',
    first_push   INTEGER NOT NULL DEFAULT 1,
    bark_enabled INTEGER NOT NULL DEFAULT 0,
    bark_sound   TEXT    NOT NULL DEFAULT '',
    bark_last_at INTEGER NOT NULL DEFAULT 0,
    flip_overrides_json TEXT NOT NULL DEFAULT '',
    fail_count   INTEGER NOT NULL DEFAULT 0,
    last_run_at  INTEGER,
    last_count   INTEGER,
    last_hash    TEXT,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sub_due ON subscriptions (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_sub_qq  ON subscriptions (qq);
CREATE INDEX IF NOT EXISTS idx_sub_fp  ON subscriptions (fingerprint);

CREATE TABLE IF NOT EXISTS seen (
    sub_id        INTEGER NOT NULL,
    item_id       TEXT    NOT NULL,
    first_seen_at INTEGER NOT NULL,
    PRIMARY KEY (sub_id, item_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id      INTEGER NOT NULL,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    matched     INTEGER NOT NULL,
    pushed      INTEGER NOT NULL,
    new_count   INTEGER NOT NULL,
    requests    INTEGER NOT NULL,
    error       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_sub ON runs (sub_id, id DESC);

CREATE TABLE IF NOT EXISTS tokens (
    qq              INTEGER PRIMARY KEY,
    cookies_json    TEXT    NOT NULL,
    user_agent      TEXT    NOT NULL DEFAULT '',
    platform        TEXT    NOT NULL DEFAULT '',
    method          TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'unknown',
    last_error      TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    last_checked_at INTEGER
);

CREATE TABLE IF NOT EXISTS user_bark (
    qq             INTEGER PRIMARY KEY,
    server         TEXT    NOT NULL DEFAULT 'https://api.day.app',
    device_key_enc BLOB    NOT NULL,
    sound          TEXT    NOT NULL DEFAULT 'alarm',
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_sent_at   INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS purchases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    qq             INTEGER NOT NULL,
    mode           TEXT    NOT NULL,
    action         TEXT    NOT NULL,
    item_name      TEXT    NOT NULL DEFAULT '',
    item_type      TEXT    NOT NULL DEFAULT '',
    item_id        TEXT    NOT NULL DEFAULT '',
    n8_id          TEXT    NOT NULL DEFAULT '',
    price          REAL,
    seller         TEXT    NOT NULL DEFAULT '',
    enc            TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'prepared',
    result_code    TEXT    NOT NULL DEFAULT '',
    result_message TEXT    NOT NULL DEFAULT '',
    raw_json       TEXT    NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchases_qq ON purchases (qq, id DESC);

-- ── 订阅抓取分析（仅超越武器）──────────────────────────────

CREATE TABLE IF NOT EXISTS crawl_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL,
    item_type    TEXT    NOT NULL,
    item_id      TEXT    NOT NULL,
    item_name    TEXT    NOT NULL,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER NOT NULL,
    matched      INTEGER NOT NULL,
    requests     INTEGER NOT NULL,
    complete     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_item ON crawl_runs (item_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_started ON crawl_runs (started_at);

CREATE TABLE IF NOT EXISTS sub_listings (
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
);
CREATE INDEX IF NOT EXISTS idx_sub_listings_active
    ON sub_listings (sub_id, item_id, state);
CREATE INDEX IF NOT EXISTS idx_sub_listings_identity
    ON sub_listings (item_id, identity_key, state);
CREATE INDEX IF NOT EXISTS idx_sub_listings_seller
    ON sub_listings (item_id, identity_key, seller_key);

CREATE TABLE IF NOT EXISTS disappeared_history (
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
);
CREATE INDEX IF NOT EXISTS idx_disappeared_identity
    ON disappeared_history (item_key, identity_key);
CREATE INDEX IF NOT EXISTS idx_disappeared_seller
    ON disappeared_history (item_key, identity_key, seller_key, disappeared_at DESC);
CREATE INDEX IF NOT EXISTS idx_disappeared_qq
    ON disappeared_history (qq, disappeared_at);

CREATE TABLE IF NOT EXISTS item_sellers (
    item_id             TEXT    NOT NULL,
    identity_key        TEXT    NOT NULL,
    item_name           TEXT    NOT NULL DEFAULT '',
    seller_key          TEXT    NOT NULL,
    seller_name         TEXT    NOT NULL DEFAULT '',
    is_original         INTEGER NOT NULL DEFAULT 0,
    first_seen_at       INTEGER NOT NULL,
    last_seen_at        INTEGER NOT NULL,
    last_disappeared_at INTEGER,
    listing_count       INTEGER NOT NULL DEFAULT 1,
    flip_in_count       INTEGER NOT NULL DEFAULT 0,
    price_min           REAL,
    price_max           REAL,
    price_last          REAL,
    quality             INTEGER,
    PRIMARY KEY (item_id, identity_key, seller_key)
);
CREATE INDEX IF NOT EXISTS idx_item_sellers_original
    ON item_sellers (item_id, identity_key, is_original);
CREATE INDEX IF NOT EXISTS idx_item_sellers_seller
    ON item_sellers (seller_key, item_id);

CREATE TABLE IF NOT EXISTS seller_flips (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id                 TEXT    NOT NULL,
    item_name               TEXT    NOT NULL DEFAULT '',
    identity_key            TEXT    NOT NULL,
    original_seller_key     TEXT    NOT NULL,
    original_seller         TEXT    NOT NULL DEFAULT '',
    prev_seller_key         TEXT    NOT NULL,
    prev_seller             TEXT    NOT NULL DEFAULT '',
    reseller_key            TEXT    NOT NULL,
    reseller                TEXT    NOT NULL DEFAULT '',
    source_disappeared_id   INTEGER NOT NULL,
    source_disappeared_at   INTEGER NOT NULL,
    reseller_first_seen_at  INTEGER NOT NULL,
    gap_sec                 INTEGER NOT NULL,
    source_price            REAL,
    reseller_price          REAL,
    quality                 INTEGER,
    identity_json           TEXT    NOT NULL DEFAULT '',
    reseller_listing_key    TEXT    NOT NULL DEFAULT '',
    reseller_last_seen_at   INTEGER,
    reseller_disappeared_at INTEGER,
    verified                INTEGER NOT NULL DEFAULT 1,
    created_at              INTEGER NOT NULL,
    UNIQUE (identity_key, reseller_listing_key)
);
CREATE INDEX IF NOT EXISTS idx_seller_flips_item
    ON seller_flips (item_id, identity_key, reseller_first_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_seller_flips_reseller
    ON seller_flips (reseller_key, reseller_first_seen_at DESC);

CREATE VIEW IF NOT EXISTS v_disappeared_by_item AS
SELECT item_key, item_name,
       COUNT(*) AS disappeared_count,
       MAX(disappeared_at) AS last_disappeared_at
FROM disappeared_history
GROUP BY item_key, item_name;

CREATE VIEW IF NOT EXISTS v_identity_groups AS
SELECT item_id AS item_key, item_name, identity_key,
       COUNT(*) AS listings,
       COUNT(DISTINCT seller_key) AS sellers,
       SUM(appear_count) AS appearances,
       SUM(disappear_count) AS disappearances,
       MIN(first_seen_at) AS first_seen_at,
       MAX(last_seen_at) AS last_seen_at,
       MAX(disappeared_at) AS last_disappeared_at
FROM sub_listings
WHERE identity_key <> ''
GROUP BY item_id, item_name, identity_key;

CREATE VIEW IF NOT EXISTS v_disappeared_identity AS
SELECT item_key, item_name, identity_key,
       COUNT(*) AS disappeared_count,
       COUNT(DISTINCT seller_key) AS seller_count,
       MIN(disappeared_at) AS first_disappeared_at,
       MAX(disappeared_at) AS last_disappeared_at
FROM disappeared_history
WHERE identity_key <> ''
GROUP BY item_key, item_name, identity_key;

-- ── 后台权限（由 admin 服务写入，bot 读取）──────────────────

CREATE TABLE IF NOT EXISTS blocked_users (
    qq            INTEGER PRIMARY KEY,
    reason        TEXT    NOT NULL DEFAULT '',
    blocked_until INTEGER,
    created_by    INTEGER,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_permissions (
    qq                INTEGER PRIMARY KEY,
    max_subscriptions INTEGER,
    allow_subscribe   INTEGER NOT NULL DEFAULT 1,
    allow_login       INTEGER NOT NULL DEFAULT 1,
    allow_trade       INTEGER NOT NULL DEFAULT 1,
    allow_price_query INTEGER NOT NULL DEFAULT 1,
    note              TEXT    NOT NULL DEFAULT '',
    updated_by        INTEGER,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by INTEGER,
    updated_at INTEGER NOT NULL
);
"""


@dataclass
class Subscription:
    id: int
    qq: int
    spec: dict[str, Any]
    fingerprint: str
    interval_sec: int
    next_run_at: int
    enabled: bool
    only_new: bool
    max_push: int
    quiet_hours: str
    first_push: bool
    fail_count: int
    last_run_at: int | None
    last_count: int | None
    last_hash: str | None
    created_at: int
    bark_enabled: bool = False
    bark_sound: str = ""
    bark_last_at: int = 0
    flip_overrides: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Subscription":
        keys = row.keys()
        raw_flip = str(row["flip_overrides_json"] or "") if "flip_overrides_json" in keys else ""
        try:
            flip_overrides = json.loads(raw_flip) if raw_flip else {}
        except (json.JSONDecodeError, TypeError):
            flip_overrides = {}
        if not isinstance(flip_overrides, dict):
            flip_overrides = {}
        return cls(
            id=int(row["id"]),
            qq=int(row["qq"]),
            spec=json.loads(row["spec_json"]),
            fingerprint=str(row["fingerprint"]),
            interval_sec=int(row["interval_sec"]),
            next_run_at=int(row["next_run_at"]),
            enabled=bool(row["enabled"]),
            only_new=bool(row["only_new"]),
            max_push=int(row["max_push"]),
            quiet_hours=str(row["quiet_hours"]),
            first_push=bool(row["first_push"]),
            bark_enabled=bool(row["bark_enabled"]) if "bark_enabled" in keys else False,
            bark_sound=str(row["bark_sound"] or "") if "bark_sound" in keys else "",
            bark_last_at=int(row["bark_last_at"] or 0) if "bark_last_at" in keys else 0,
            flip_overrides=flip_overrides,
            fail_count=int(row["fail_count"]),
            last_run_at=row["last_run_at"],
            last_count=row["last_count"],
            last_hash=row["last_hash"],
            created_at=int(row["created_at"]),
        )


@dataclass
class UserBark:
    """某个 QQ 用户的 Bark 配置；device key 只保存 DPAPI 密文。"""

    qq: int
    server: str
    device_key_enc: bytes
    sound: str
    enabled: bool
    last_sent_at: int
    last_error: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "UserBark":
        return cls(
            qq=int(row["qq"]),
            server=str(row["server"] or ""),
            device_key_enc=bytes(row["device_key_enc"] or b""),
            sound=str(row["sound"] or ""),
            enabled=bool(row["enabled"]),
            last_sent_at=int(row["last_sent_at"] or 0),
            last_error=str(row["last_error"] or ""),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass
class Token:
    """某个 QQ 用户绑定的 Nexon 登录态（Cookie 字典，不含密码）。"""

    qq: int
    cookies: dict[str, str]
    user_agent: str
    platform: str
    method: str
    status: str
    last_error: str
    created_at: int
    updated_at: int
    last_checked_at: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Token":
        try:
            cookies = json.loads(row["cookies_json"])
        except (json.JSONDecodeError, TypeError):
            cookies = {}
        return cls(
            qq=int(row["qq"]),
            cookies={str(k): str(v) for k, v in (cookies or {}).items()},
            user_agent=str(row["user_agent"] or ""),
            platform=str(row["platform"] or ""),
            method=str(row["method"] if "method" in row.keys() else ""),
            status=str(row["status"] or "unknown"),
            last_error=str(row["last_error"] or ""),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_checked_at=row["last_checked_at"],
        )

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


@dataclass
class Purchase:
    """一次采购/组队请求的记录。enc 是服务端确认凭证，不对外展示。"""

    id: int
    qq: int
    mode: str
    action: str
    item_name: str
    item_type: str
    item_id: str
    n8_id: str
    price: float | None
    seller: str
    enc: str
    status: str
    result_code: str
    result_message: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Purchase":
        return cls(
            id=int(row["id"]),
            qq=int(row["qq"]),
            mode=str(row["mode"]),
            action=str(row["action"]),
            item_name=str(row["item_name"] or ""),
            item_type=str(row["item_type"] or ""),
            item_id=str(row["item_id"] or ""),
            n8_id=str(row["n8_id"] or ""),
            price=float(row["price"]) if row["price"] is not None else None,
            seller=str(row["seller"] or ""),
            enc=str(row["enc"] or ""),
            status=str(row["status"] or ""),
            result_code=str(row["result_code"] or ""),
            result_message=str(row["result_message"] or ""),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class Store:
    """订阅数据仓库。path 为 SQLite 文件路径，`:memory:` 可用于测试。"""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """给旧版 tokens/subscriptions 补列，并清理已废弃的 storage_state_json。"""
        columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(tokens)")
        }
        if "method" not in columns:
            self._conn.execute(
                "ALTER TABLE tokens ADD COLUMN method TEXT NOT NULL DEFAULT ''"
            )
        if "storage_state_json" in columns:
            self._conn.execute("ALTER TABLE tokens DROP COLUMN storage_state_json")

        sub_columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(subscriptions)")
        }
        for column, ddl in (
            ("bark_enabled", "ALTER TABLE subscriptions ADD COLUMN bark_enabled INTEGER NOT NULL DEFAULT 0"),
            ("bark_sound", "ALTER TABLE subscriptions ADD COLUMN bark_sound TEXT NOT NULL DEFAULT ''"),
            ("bark_last_at", "ALTER TABLE subscriptions ADD COLUMN bark_last_at INTEGER NOT NULL DEFAULT 0"),
            ("flip_overrides_json", "ALTER TABLE subscriptions ADD COLUMN flip_overrides_json TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in sub_columns:
                self._conn.execute(ddl)

        listing_columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(sub_listings)")
        }
        if "seller_key" not in listing_columns:
            self._conn.execute(
                "ALTER TABLE sub_listings ADD COLUMN seller_key TEXT NOT NULL DEFAULT ''"
            )
        history_columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(disappeared_history)")
        }
        if "seller_key" not in history_columns:
            self._conn.execute(
                "ALTER TABLE disappeared_history ADD COLUMN seller_key TEXT NOT NULL DEFAULT ''"
            )

    # ── 用户 ────────────────────────────────────────────────

    def upsert_user(self, qq: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (qq, created_at) VALUES (?, ?)",
                (qq, int(time.time())),
            )

    # ── 订阅 ────────────────────────────────────────────────

    def count_subscriptions(self, qq: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE qq = ?", (qq,)
            ).fetchone()
        return int(row[0])

    def add_subscription(
        self,
        qq: int,
        spec: dict[str, Any],
        fingerprint: str,
        *,
        interval_sec: int,
        max_push: int,
        only_new: bool,
        quiet_hours: str,
        first_push: bool,
        bark_sound: str = "",
        next_run_at: int | None = None,
    ) -> int:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO subscriptions (
                    qq, spec_json, fingerprint, interval_sec, next_run_at,
                    enabled, only_new, max_push, quiet_hours, first_push,
                    bark_sound, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qq,
                    json.dumps(spec, ensure_ascii=False),
                    fingerprint,
                    interval_sec,
                    now + 15 if next_run_at is None else next_run_at,
                    int(only_new),
                    max_push,
                    quiet_hours,
                    int(first_push),
                    bark_sound,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_subscription(self, sub_id: int, qq: int | None = None) -> Subscription | None:
        sql = "SELECT * FROM subscriptions WHERE id = ?"
        args: list[Any] = [sub_id]
        if qq is not None:
            sql += " AND qq = ?"
            args.append(qq)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return Subscription.from_row(row) if row else None

    def find_by_fingerprint(self, qq: int, fingerprint: str) -> Subscription | None:
        """查找同一用户已存在的相同条件订阅，避免崩溃后重试产生重复。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE qq = ? AND fingerprint = ? ORDER BY id LIMIT 1",
                (qq, fingerprint),
            ).fetchone()
        return Subscription.from_row(row) if row else None

    def list_subscriptions(self, qq: int) -> list[Subscription]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subscriptions WHERE qq = ? ORDER BY id", (qq,)
            ).fetchall()
        return [Subscription.from_row(r) for r in rows]

    def due_subscriptions(self, now: int, limit: int = 100) -> list[Subscription]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [Subscription.from_row(r) for r in rows]

    def set_enabled(self, sub_id: int, qq: int, enabled: bool) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE subscriptions SET enabled = ? WHERE id = ? AND qq = ?",
                (int(enabled), sub_id, qq),
            )
            return cur.rowcount > 0

    def delete_subscription(self, sub_id: int, qq: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE id = ? AND qq = ?", (sub_id, qq)
            )
            if cur.rowcount <= 0:
                return False
            self._conn.execute("DELETE FROM seen WHERE sub_id = ?", (sub_id,))
            return True

    def update_options(self, sub_id: int, qq: int, fields: dict[str, Any]) -> bool:
        allowed = {
            "interval_sec", "max_push", "only_new", "quiet_hours", "first_push",
            "bark_enabled", "bark_sound", "flip_overrides_json",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        sql = "UPDATE subscriptions SET " + ", ".join(f"{k} = ?" for k in sets)
        args = list(sets.values()) + [sub_id, qq]
        with self._lock, self._conn:
            cur = self._conn.execute(sql + " WHERE id = ? AND qq = ?", args)
            return cur.rowcount > 0

    def schedule_now(self, sub_id: int, qq: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE subscriptions SET next_run_at = ? WHERE id = ? AND qq = ?",
                (int(time.time()), sub_id, qq),
            )
            return cur.rowcount > 0

    def reschedule(self, sub_id: int, next_run_at: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscriptions SET next_run_at = ? WHERE id = ?",
                (next_run_at, sub_id),
            )

    def recover_stale_backoff(self, now: int | None = None, delay: int = 60) -> int:
        """启动时清理上次运行留下的失败退避。

        机器人和 NapCat 都可能重启，旧进程的指数退避不应继续生效：
        把 fail_count 归零，并把下次运行提前到 delay 秒内。
        """
        stamp = int(now if now is not None else time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE subscriptions SET fail_count = 0, next_run_at = MIN(next_run_at, ?) "
                "WHERE fail_count > 0",
                (stamp + delay,),
            )
            return cur.rowcount

    # ── 去重 ────────────────────────────────────────────────

    def check_new(self, sub_id: int, item_ids: list[str]) -> set[str]:
        """只读检查：返回 item_ids 中尚未记录为 seen 的 ID。"""
        if not item_ids:
            return set()
        new_ids: set[str] = set()
        with self._lock:
            for start in range(0, len(item_ids), 400):
                chunk = item_ids[start:start + 400]
                placeholders = ",".join("?" * len(chunk))
                existing = {
                    str(r[0])
                    for r in self._conn.execute(
                        f"SELECT item_id FROM seen WHERE sub_id = ? AND item_id IN ({placeholders})",
                        (sub_id, *chunk),
                    )
                }
                new_ids.update(i for i in chunk if i not in existing)
        return new_ids

    def mark_seen(self, sub_id: int, item_ids: list[str], now: int | None = None) -> None:
        """写入 seen。调度器在推送成功后才调用，保证只推新模式不丢消息。"""
        if not item_ids:
            return
        stamp = int(now if now is not None else time.time())
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen (sub_id, item_id, first_seen_at) VALUES (?, ?, ?)",
                [(sub_id, i, stamp) for i in item_ids],
            )

    def seen_new(self, sub_id: int, item_ids: list[str], now: int | None = None) -> set[str]:
        """检查并写入 seen，返回其中首次出现的 ID（测试/一次性场景用）。"""
        new_ids = self.check_new(sub_id, item_ids)
        self.mark_seen(sub_id, item_ids, now)
        return new_ids

    def prune_seen(self, days: int = 30) -> int:
        cutoff = int(time.time()) - days * 86400
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM seen WHERE first_seen_at < ?", (cutoff,)
            )
            return cur.rowcount

    def prune_runs(self, days: int = 30) -> int:
        """删除过期运行记录，避免 runs 表无限增长。"""
        cutoff = int(time.time()) - days * 86400
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM runs WHERE started_at < ?", (cutoff,)
            )
            return cur.rowcount

    # ── 运行记录 ────────────────────────────────────────────

    def record_success(
        self,
        sub_id: int,
        *,
        started_at: int,
        matched: int,
        pushed: int,
        new_count: int,
        requests: int,
        last_hash: str,
        next_run_at: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE subscriptions
                SET last_run_at = ?, last_count = ?, last_hash = ?,
                    fail_count = 0, next_run_at = ?
                WHERE id = ?
                """,
                (started_at, matched, last_hash, next_run_at, sub_id),
            )
            self._conn.execute(
                """
                INSERT INTO runs (sub_id, started_at, finished_at, matched, pushed,
                                  new_count, requests, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, '')
                """,
                (
                    sub_id, started_at, int(time.time()), matched, pushed,
                    new_count, requests,
                ),
            )

    def record_failure(
        self,
        sub_id: int,
        *,
        started_at: int,
        error: str,
        next_run_at: int,
        fail_count: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE subscriptions
                SET fail_count = ?, next_run_at = ?
                WHERE id = ?
                """,
                (fail_count, next_run_at, sub_id),
            )
            self._conn.execute(
                """
                INSERT INTO runs (sub_id, started_at, finished_at, matched, pushed,
                                  new_count, requests, error)
                VALUES (?, ?, ?, 0, 0, 0, 0, ?)
                """,
                (sub_id, started_at, int(time.time()), error[:500]),
            )

    def recent_runs(self, sub_id: int, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM runs WHERE sub_id = ? ORDER BY id DESC LIMIT ?",
                (sub_id, limit),
            ).fetchall()

    # ── 登录令牌（按 QQ 用户隔离）──────────────────────────

    def save_token(
        self,
        qq: int,
        cookies: dict[str, str],
        *,
        user_agent: str = "",
        platform: str = "",
        method: str = "",
        status: str = "valid",
        error: str = "",
    ) -> None:
        """保存/覆盖该用户的登录 Cookie。旧 token 会被新登录替换。"""
        now = int(time.time())
        payload = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tokens (
                    qq, cookies_json, user_agent, platform, method,
                    status, last_error, created_at, updated_at, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(qq) DO UPDATE SET
                    cookies_json = excluded.cookies_json,
                    user_agent = excluded.user_agent,
                    platform = excluded.platform,
                    method = excluded.method,
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at,
                    last_checked_at = excluded.last_checked_at
                """,
                (
                    qq, payload, user_agent, platform, method,
                    status, error, now, now, now,
                ),
            )

    def get_token(self, qq: int) -> Token | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tokens WHERE qq = ?", (qq,)).fetchone()
        return Token.from_row(row) if row else None

    def delete_token(self, qq: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM tokens WHERE qq = ?", (qq,))
            return cur.rowcount > 0

    def update_token_status(self, qq: int, status: str, error: str = "") -> bool:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE tokens
                SET status = ?, last_error = ?, last_checked_at = ?, updated_at = ?
                WHERE qq = ?
                """,
                (status, error, now, now, qq),
            )
            return cur.rowcount > 0

    # ── Bark 推送配置 ────────────────────────────────────────

    def get_user_bark(self, qq: int) -> UserBark | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM user_bark WHERE qq = ?", (int(qq),)
            ).fetchone()
        return UserBark.from_row(row) if row else None

    def save_user_bark(
        self,
        qq: int,
        *,
        server: str,
        device_key_enc: bytes,
        sound: str = "",
        enabled: bool = True,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO user_bark (
                    qq, server, device_key_enc, sound, enabled,
                    last_sent_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, COALESCE(NULLIF(?, ''), 'alarm'), ?, 0, '', ?, ?)
                ON CONFLICT(qq) DO UPDATE SET
                    server = excluded.server,
                    device_key_enc = excluded.device_key_enc,
                    sound = CASE WHEN ? = '' THEN user_bark.sound ELSE ? END,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    int(qq), server, device_key_enc, sound, int(enabled), now, now,
                    sound, sound,
                ),
            )

    def set_user_bark_enabled(self, qq: int, enabled: bool) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE user_bark SET enabled = ?, updated_at = ? WHERE qq = ?",
                (int(enabled), int(time.time()), int(qq)),
            )
            return cur.rowcount > 0

    def set_user_bark_sound(self, qq: int, sound: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE user_bark SET sound = ?, updated_at = ? WHERE qq = ?",
                (sound, int(time.time()), int(qq)),
            )
            return cur.rowcount > 0

    def delete_user_bark(self, qq: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM user_bark WHERE qq = ?", (int(qq),))
            return cur.rowcount > 0

    def mark_bark_sent(self, sub_id: int, qq: int, ts: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscriptions SET bark_last_at = ? WHERE id = ?",
                (int(ts), int(sub_id)),
            )
            self._conn.execute(
                "UPDATE user_bark SET last_sent_at = ?, last_error = '', updated_at = ? "
                "WHERE qq = ?",
                (int(ts), int(ts), int(qq)),
            )

    def mark_bark_error(self, qq: int, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE user_bark SET last_error = ?, updated_at = ? WHERE qq = ?",
                (str(error)[:200], int(time.time()), int(qq)),
            )

    # ── 采购/组队记录 ───────────────────────────────────────

    def add_purchase(
        self,
        qq: int,
        *,
        mode: str,
        action: str,
        item_name: str,
        item_type: str,
        item_id: str,
        n8_id: str,
        price: float | None,
        seller: str = "",
        enc: str = "",
        status: str = "prepared",
        result_code: str = "",
        result_message: str = "",
        raw: dict[str, Any] | None = None,
    ) -> int:
        now = int(time.time())
        raw_json = json.dumps(raw or {}, ensure_ascii=False) if raw else ""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO purchases (
                    qq, mode, action, item_name, item_type, item_id, n8_id,
                    price, seller, enc, status, result_code, result_message,
                    raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    qq, mode, action, item_name, item_type, item_id, n8_id,
                    price, seller, enc, status, result_code, result_message,
                    raw_json, now, now,
                ),
            )
            return int(cur.lastrowid)

    def get_purchase(self, purchase_id: int, qq: int | None = None) -> Purchase | None:
        sql = "SELECT * FROM purchases WHERE id = ?"
        args: list[Any] = [purchase_id]
        if qq is not None:
            sql += " AND qq = ?"
            args.append(qq)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return Purchase.from_row(row) if row else None

    def update_purchase(
        self,
        purchase_id: int,
        qq: int,
        *,
        status: str | None = None,
        seller: str | None = None,
        enc: str | None = None,
        result_code: str | None = None,
        result_message: str | None = None,
    ) -> bool:
        sets: dict[str, Any] = {}
        if status is not None:
            sets["status"] = status
        if seller is not None:
            sets["seller"] = seller
        if enc is not None:
            sets["enc"] = enc
        if result_code is not None:
            sets["result_code"] = result_code
        if result_message is not None:
            sets["result_message"] = result_message
        if not sets:
            return False
        sets["updated_at"] = int(time.time())
        sql = "UPDATE purchases SET " + ", ".join(f"{k} = ?" for k in sets)
        args = list(sets.values()) + [purchase_id, qq]
        with self._lock, self._conn:
            cur = self._conn.execute(sql + " WHERE id = ? AND qq = ?", args)
            return cur.rowcount > 0

    def list_purchases(self, qq: int, limit: int = 10) -> list[Purchase]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM purchases WHERE qq = ? ORDER BY id DESC LIMIT ?",
                (qq, max(1, limit)),
            ).fetchall()
        return [Purchase.from_row(r) for r in rows]

    def prune_purchases(self, days: int = 90) -> int:
        cutoff = int(time.time()) - days * 86400
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM purchases WHERE created_at < ?", (cutoff,)
            )
            return cur.rowcount

    # ── 订阅抓取分析（仅超越武器）──────────────────────────

    def create_crawl_run(
        self,
        *,
        fingerprint: str,
        item_type: str,
        item_id: str,
        item_name: str,
        started_at: int,
        finished_at: int,
        matched: int,
        requests: int,
        complete: bool,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO crawl_runs (
                    fingerprint, item_type, item_id, item_name, started_at,
                    finished_at, matched, requests, complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint, item_type, item_id, item_name, started_at,
                    finished_at, matched, requests, int(complete),
                ),
            )
            return int(cur.lastrowid)

    def list_sub_listings(self, sub_id: int, item_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sub_listings WHERE sub_id = ? AND item_id = ? ORDER BY id",
                (sub_id, item_id),
            ).fetchall()

    def list_active_sub_listings(self, sub_id: int, item_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sub_listings "
                "WHERE sub_id = ? AND item_id = ? AND state = 'active' ORDER BY id",
                (sub_id, item_id),
            ).fetchall()

    def insert_sub_listing(
        self,
        sub_id: int,
        qq: int,
        item: Any,
        snapshot: Any,
        run_id: int,
        now: int,
    ) -> None:
        item_type = str(item.item_type)
        item_id = str(item.item_id)
        item_name = item.name("cn")
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO sub_listings (
                    sub_id, qq, item_type, item_id, item_name, n8_id,
                    identity_key, identity_json, state,
                    first_seen_at, last_seen_at, first_run_id, last_run_id,
                    appear_count, disappear_count, dt_register,
                    price_first, price_last, seller_first, seller_last,
                    seller_key, quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sub_id, qq, item_type, item_id, item_name, snapshot.n8_id,
                    snapshot.identity_key, snapshot.identity_json, "active",
                    now, now, run_id, run_id, 1, 0, snapshot.dt_register,
                    snapshot.price, snapshot.price, snapshot.seller, snapshot.seller,
                    snapshot.seller_key, snapshot.quality,
                ),
            )

    def mark_sub_listing_seen(
        self,
        sub_id: int,
        item_id: str,
        n8_id: str,
        snapshot: Any,
        run_id: int,
        now: int,
        *,
        reappeared: bool = False,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE sub_listings SET
                    last_seen_at = ?, last_run_id = ?, price_last = ?,
                    seller_first = CASE WHEN seller_first = '' THEN ? ELSE seller_first END,
                    seller_last = ?, seller_key = ?,
                    dt_register = ?, state = 'active', disappeared_at = NULL,
                    appear_count = appear_count + ?,
                    identity_key = ?, identity_json = ?
                WHERE sub_id = ? AND item_id = ? AND n8_id = ?
                """,
                (
                    now, run_id, snapshot.price, snapshot.seller,
                    snapshot.seller, snapshot.seller_key, snapshot.dt_register,
                    1 if reappeared else 0,
                    snapshot.identity_key, snapshot.identity_json,
                    sub_id, item_id, n8_id,
                ),
            )

    def mark_sub_listing_disappeared(
        self,
        row: sqlite3.Row,
        item_name: str,
        run_id: int,
        now: int,
        spec_json: str,
    ) -> int:
        item_id = str(row["item_id"])
        identity_key = str(row["identity_key"] or "")
        name = str(row["seller_last"] or "")
        key = str(row["seller_key"] or "") or seller_key(name)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE sub_listings SET
                    state = 'disappeared', disappeared_at = ?,
                    disappear_count = disappear_count + 1, last_run_id = ?
                WHERE id = ?
                """,
                (now, run_id, int(row["id"])),
            )
            cur = self._conn.execute(
                """
                INSERT INTO disappeared_history (
                    item_key, item_type, item_id, item_name, sub_id, qq,
                    identity_key, identity_json, first_seen_at, last_seen_at,
                    disappeared_at, run_id, price, seller, seller_key, dt_register,
                    quality, spec_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id, str(row["item_type"]), item_id, item_name,
                    int(row["sub_id"]), int(row["qq"]),
                    identity_key, str(row["identity_json"] or ""),
                    int(row["first_seen_at"]), int(row["last_seen_at"]), now, run_id,
                    row["price_last"], name, key,
                    str(row["dt_register"] or ""), row["quality"], spec_json,
                ),
            )
            if identity_key and key:
                self._conn.execute(
                    "UPDATE item_sellers SET last_disappeared_at = ? "
                    "WHERE item_id = ? AND identity_key = ? AND seller_key = ?",
                    (now, item_id, identity_key, key),
                )
            if identity_key:
                self._conn.execute(
                    "UPDATE seller_flips SET "
                    "reseller_last_seen_at = COALESCE(reseller_last_seen_at, ?), "
                    "reseller_disappeared_at = ? "
                    "WHERE identity_key = ? AND reseller_listing_key = ?",
                    (
                        int(row["last_seen_at"]), now, identity_key,
                        str(row["n8_id"]),
                    ),
                )
            return int(cur.lastrowid)

    # ── 卖家与导狗 ──────────────────────────────────────────

    def upsert_item_seller(
        self,
        item_id: str,
        identity_key: str,
        item_name: str,
        seller_key_value: str,
        seller_name: str,
        *,
        now: int,
        price: float | None,
        quality: int | None,
        new_listing: bool,
    ) -> None:
        """记录卖家在某商品身份下的上架情况；首次出现的卖家自动成为原始卖家。"""
        if not identity_key or not seller_key_value:
            return
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO item_sellers (
                    item_id, identity_key, item_name, seller_key, seller_name,
                    is_original, first_seen_at, last_seen_at,
                    listing_count, flip_in_count,
                    price_min, price_max, price_last, quality
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    item_id, identity_key, item_name, seller_key_value, seller_name,
                    now, now, 1 if new_listing else 0,
                    price, price, price, quality,
                ),
            )
            if cur.rowcount:
                other = self._conn.execute(
                    "SELECT 1 FROM item_sellers WHERE item_id = ? AND identity_key = ? "
                    "AND seller_key <> ? LIMIT 1",
                    (item_id, identity_key, seller_key_value),
                ).fetchone()
                if other is None:
                    self._conn.execute(
                        "UPDATE item_sellers SET is_original = 1 "
                        "WHERE item_id = ? AND identity_key = ? AND seller_key = ?",
                        (item_id, identity_key, seller_key_value),
                    )
                return
            self._conn.execute(
                """
                UPDATE item_sellers SET
                    seller_name = ?,
                    item_name = ?,
                    last_seen_at = ?,
                    first_seen_at = MIN(first_seen_at, ?),
                    listing_count = listing_count + ?,
                    price_min = CASE WHEN price_min IS NULL OR ? < price_min THEN ? ELSE price_min END,
                    price_max = CASE WHEN price_max IS NULL OR ? > price_max THEN ? ELSE price_max END,
                    price_last = ?,
                    quality = COALESCE(?, quality)
                WHERE item_id = ? AND identity_key = ? AND seller_key = ?
                """,
                (
                    seller_name, item_name, now, now, 1 if new_listing else 0,
                    price, price, price, price, price, quality,
                    item_id, identity_key, seller_key_value,
                ),
            )

    def list_item_sellers(self, item_id: str, identity_key: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM item_sellers WHERE item_id = ? AND identity_key = ? "
                "ORDER BY is_original DESC, first_seen_at, seller_key",
                (item_id, identity_key),
            ).fetchall()

    def get_original_seller(self, item_id: str, identity_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM item_sellers WHERE item_id = ? AND identity_key = ? "
                "ORDER BY is_original DESC, first_seen_at, seller_key LIMIT 1",
                (item_id, identity_key),
            ).fetchone()

    def original_last_disappeared(
        self,
        item_key: str,
        identity_key: str,
        seller_key_value: str,
        before: int,
    ) -> sqlite3.Row | None:
        """原始卖家在该商品身份下、指定时间之前最后一次消失。"""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM disappeared_history "
                "WHERE item_key = ? AND identity_key = ? AND seller_key = ? "
                "AND disappeared_at <= ? ORDER BY disappeared_at DESC, id DESC LIMIT 1",
                (item_key, identity_key, seller_key_value, int(before)),
            ).fetchone()

    def latest_disappeared(
        self,
        item_key: str,
        identity_key: str,
        before: int,
        exclude_seller_key: str,
    ) -> sqlite3.Row | None:
        """在导狗上架之前，该商品身份最后一次消失记录（排除导狗本人）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM disappeared_history "
                "WHERE item_key = ? AND identity_key = ? AND disappeared_at <= ? "
                "AND seller_key <> ? ORDER BY disappeared_at DESC, id DESC LIMIT 1",
                (item_key, identity_key, int(before), exclude_seller_key),
            ).fetchone()

    def insert_seller_flip(self, record: dict[str, Any]) -> int:
        columns = (
            "item_id", "item_name", "identity_key",
            "original_seller_key", "original_seller",
            "prev_seller_key", "prev_seller",
            "reseller_key", "reseller",
            "source_disappeared_id", "source_disappeared_at",
            "reseller_first_seen_at", "gap_sec",
            "source_price", "reseller_price", "quality", "identity_json",
            "reseller_listing_key", "reseller_last_seen_at",
            "reseller_disappeared_at", "verified", "created_at",
        )
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seller_flips (" + ", ".join(columns) + ") "
                "VALUES (" + ", ".join("?" for _ in columns) + ")",
                tuple(record[name] for name in columns),
            )
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE item_sellers SET flip_in_count = flip_in_count + 1 "
                    "WHERE item_id = ? AND identity_key = ? AND seller_key = ?",
                    (record["item_id"], record["identity_key"], record["reseller_key"]),
                )
            return int(cur.rowcount)

    def mark_flip_reseller_state(
        self, identity_key: str, listing_key: str, *, last_seen_at: int | None = None,
        disappeared_at: int | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[Any] = []
        if last_seen_at is not None:
            sets.append("reseller_last_seen_at = ?")
            args.append(int(last_seen_at))
        if disappeared_at is not None:
            sets.append("reseller_disappeared_at = ?")
            args.append(int(disappeared_at))
        if not sets or not listing_key:
            return
        args.extend([identity_key, listing_key])
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE seller_flips SET " + ", ".join(sets)
                + " WHERE identity_key = ? AND reseller_listing_key = ?",
                tuple(args),
            )

    def list_crawl_runs(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()

    def prune_crawl_runs(self, days: int = 30) -> int:
        cutoff = int(time.time()) - days * 86400
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM crawl_runs WHERE started_at < ?", (cutoff,)
            )
            return cur.rowcount

    # ── 后台权限（只读）────────────────────────────────────

    def get_blocked_user(self, qq: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM blocked_users WHERE qq = ?", (qq,)
            ).fetchone()

    def get_user_permissions(self, qq: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM user_permissions WHERE qq = ?", (qq,)
            ).fetchone()

    def get_system_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value_json FROM system_settings"
            ).fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            try:
                settings[str(row["key"])] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return settings

    # ── 生命周期 ────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()
