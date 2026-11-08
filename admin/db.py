"""后台管理服务的数据库访问：admin.db（凭据/会话/审计）与 qqbot.db（权限/设置）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

ADMIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'super_admin',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    token_hash  TEXT    NOT NULL UNIQUE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    ip          TEXT    NOT NULL DEFAULT '',
    user_agent  TEXT    NOT NULL DEFAULT '',
    revoked_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions (admin_id, id DESC);

CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    target_type TEXT    NOT NULL DEFAULT '',
    target_id   TEXT    NOT NULL DEFAULT '',
    before_json TEXT    NOT NULL DEFAULT '',
    after_json  TEXT    NOT NULL DEFAULT '',
    ip          TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_time ON admin_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_admin ON admin_audit (admin_id, id DESC);
"""

TRADE_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_stats_items (
    item_id        TEXT PRIMARY KEY,
    item_type      TEXT NOT NULL DEFAULT 'transcendent',
    item_name      TEXT NOT NULL DEFAULT '',
    last_crawled_at INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trade_stats_points (
    item_id       TEXT NOT NULL,
    bucket_at     INTEGER NOT NULL,
    average_price REAL NOT NULL,
    trade_count   INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (item_id, bucket_at)
);
CREATE INDEX IF NOT EXISTS idx_trade_stats_points_time ON trade_stats_points (bucket_at DESC);

CREATE TABLE IF NOT EXISTS trade_stats_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    items       INTEGER NOT NULL,
    inserted    INTEGER NOT NULL,
    failed      INTEGER NOT NULL,
    error       TEXT NOT NULL DEFAULT ''
);
"""

BOT_PERMISSION_SCHEMA = """
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
"""


def _open(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if read_only:
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=10,
        )
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=10
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


class WriteConflictError(RuntimeError):
    """重试后仍然无法取得写锁。"""


class AdminDB:
    """管理端凭据、会话与审计；独立于 bot 数据库。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = _open(self.path)
        with self._lock, self._conn:
            self._conn.executescript(ADMIN_SCHEMA)
            self._conn.executescript(TRADE_STATS_SCHEMA)

    def _write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        for attempt in range(5):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.execute("COMMIT")
                        return result
                    except Exception:
                        self._conn.execute("ROLLBACK")
                        raise
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= 4:
                    raise WriteConflictError(str(exc)) from exc
                time.sleep(0.05 * (2**attempt))
        raise WriteConflictError("database is locked")

    # ── 管理员 ──────────────────────────────────────────────

    def count_admins(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])

    def create_admin(
        self,
        username: str,
        password_hash: str,
        *,
        role: str = "super_admin",
    ) -> int:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO admin_users (username, password_hash, role, disabled, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (username.strip(), password_hash, role, now),
            )
            return int(cur.lastrowid)

        return int(self._write(run))

    def get_admin_by_username(self, username: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM admin_users WHERE username = ?", (username.strip(),)
            ).fetchone()

    def get_admin(self, admin_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM admin_users WHERE id = ?", (admin_id,)
            ).fetchone()

    def mark_login(self, admin_id: int) -> None:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE admin_users SET last_login_at = ? WHERE id = ?", (now, admin_id)
            )

        self._write(run)

    def update_password(self, admin_id: int, password_hash: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE admin_users SET password_hash = ? WHERE id = ?",
                (password_hash, admin_id),
            )

        self._write(run)

    # ── 会话 ────────────────────────────────────────────────

    def create_session(
        self,
        admin_id: int,
        token_hash: str,
        *,
        expires_at: int,
        ip: str = "",
        user_agent: str = "",
    ) -> int:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO admin_sessions "
                "(admin_id, token_hash, created_at, expires_at, last_seen_at, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (admin_id, token_hash, now, expires_at, now, ip, user_agent[:300]),
            )
            return int(cur.lastrowid)

        return int(self._write(run))

    def get_session(self, token_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT s.*, u.username, u.role, u.disabled "
                "FROM admin_sessions AS s JOIN admin_users AS u ON u.id = s.admin_id "
                "WHERE s.token_hash = ?",
                (token_hash,),
            ).fetchone()

    def touch_session(self, session_id: int, now: int) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE admin_sessions SET last_seen_at = ? WHERE id = ?",
                (now, session_id),
            )

        self._write(run)

    def revoke_session(self, session_id: int) -> None:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE admin_sessions SET revoked_at = ? WHERE id = ?",
                (now, session_id),
            )

        self._write(run)

    def revoke_admin_sessions(self, admin_id: int, *, keep_session_id: int | None = None) -> int:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> int:
            if keep_session_id is None:
                cur = conn.execute(
                    "UPDATE admin_sessions SET revoked_at = ? "
                    "WHERE admin_id = ? AND revoked_at IS NULL",
                    (now, admin_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE admin_sessions SET revoked_at = ? "
                    "WHERE admin_id = ? AND revoked_at IS NULL AND id <> ?",
                    (now, admin_id, keep_session_id),
                )
            return int(cur.rowcount)

        return int(self._write(run))

    def purge_expired_sessions(self, now: int | None = None) -> int:
        stamp = int(now if now is not None else time.time())

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at < ? OR revoked_at IS NOT NULL",
                (stamp,),
            )
            return int(cur.rowcount)

        return int(self._write(run))

    # ── 审计 ────────────────────────────────────────────────

    def audit(
        self,
        admin_id: int,
        action: str,
        *,
        target_type: str = "",
        target_id: str = "",
        before: Any = None,
        after: Any = None,
        ip: str = "",
    ) -> int:
        now = int(time.time())
        before_json = json.dumps(before, ensure_ascii=False) if before is not None else ""
        after_json = json.dumps(after, ensure_ascii=False) if after is not None else ""

        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO admin_audit "
                "(admin_id, action, target_type, target_id, before_json, after_json, ip, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (admin_id, action, target_type, target_id, before_json, after_json, ip, now),
            )
            return int(cur.lastrowid)

        return int(self._write(run))

    def list_audit(
        self, *, limit: int = 50, offset: int = 0, action: str | None = None
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT a.*, u.username FROM admin_audit AS a "
            "LEFT JOIN admin_users AS u ON u.id = a.admin_id"
        )
        args: list[Any] = []
        if action:
            sql += " WHERE a.action = ?"
            args.append(action)
        sql += " ORDER BY a.id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ── 成交统计 ────────────────────────────────────────────

    def upsert_trade_stats_item(
        self,
        item_id: str,
        item_name: str,
        *,
        crawled_at: int,
        error: str = "",
        item_type: str = "transcendent",
    ) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO trade_stats_items "
                "(item_id, item_type, item_name, last_crawled_at, last_error) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET "
                "item_name = excluded.item_name, "
                "item_type = excluded.item_type, "
                "last_crawled_at = excluded.last_crawled_at, "
                "last_error = excluded.last_error",
                (item_id, item_type, item_name, crawled_at, error),
            )

        self._write(run)

    def insert_trade_stats_points(
        self, item_id: str, points: list[tuple[int, float, int]]
    ) -> int:
        """points: [(bucket_at, average_price, trade_count)]；已存在的小时跳过。"""
        if not points:
            return 0
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> int:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO trade_stats_points "
                "(item_id, bucket_at, average_price, trade_count, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(item_id, bucket, price, count, now) for bucket, price, count in points],
            )
            return int(conn.total_changes - before)

        return int(self._write(run))

    def list_trade_stats_items(self) -> list[sqlite3.Row]:
        now = int(time.time())
        with self._lock:
            return self._conn.execute(
                "SELECT i.item_id, i.item_type, i.item_name, i.last_crawled_at, i.last_error, "
                "(SELECT COUNT(*) FROM trade_stats_points p WHERE p.item_id = i.item_id) AS point_count, "
                "(SELECT MAX(p.bucket_at) FROM trade_stats_points p WHERE p.item_id = i.item_id) AS last_bucket_at, "
                "(SELECT SUM(p.trade_count) FROM trade_stats_points p "
                " WHERE p.item_id = i.item_id AND p.bucket_at >= ?) AS trades_24h, "
                "(SELECT SUM(p.average_price * p.trade_count) FROM trade_stats_points p "
                " WHERE p.item_id = i.item_id AND p.bucket_at >= ?) AS turnover_24h, "
                "(SELECT p.average_price FROM trade_stats_points p WHERE p.item_id = i.item_id "
                " ORDER BY p.bucket_at DESC LIMIT 1) AS last_price, "
                "(SELECT p.trade_count FROM trade_stats_points p WHERE p.item_id = i.item_id "
                " ORDER BY p.bucket_at DESC LIMIT 1) AS last_count "
                "FROM trade_stats_items AS i ORDER BY i.item_name COLLATE NOCASE",
                (now - 86400, now - 86400),
            ).fetchall()

    def get_trade_stats_item(self, item_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM trade_stats_items WHERE item_id = ?", (item_id,)
            ).fetchone()

    def list_trade_stats_points(
        self, item_id: str, *, since: int | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT bucket_at, average_price, trade_count FROM trade_stats_points WHERE item_id = ?"
        args: list[Any] = [item_id]
        if since is not None:
            sql += " AND bucket_at >= ?"
            args.append(int(since))
        sql += " ORDER BY bucket_at ASC"
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def record_trade_stats_run(
        self,
        *,
        started_at: int,
        finished_at: int,
        items: int,
        inserted: int,
        failed: int,
        error: str = "",
    ) -> int:
        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO trade_stats_runs "
                "(started_at, finished_at, items, inserted, failed, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (started_at, finished_at, items, inserted, failed, error[:500]),
            )
            return int(cur.lastrowid)

        return int(self._write(run))

    def latest_trade_stats_run(self) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM trade_stats_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class BotDB:
    """bot 数据库的权限与设置访问；读走只读连接，写走独立连接。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._write_conn = _open(self.path)
        with self._lock, self._write_conn:
            self._write_conn.executescript(BOT_PERMISSION_SCHEMA)
        self._read_conn = _open(self.path, read_only=True)

    def _write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        for attempt in range(5):
            try:
                with self._lock:
                    self._write_conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._write_conn)
                        self._write_conn.execute("COMMIT")
                        return result
                    except Exception:
                        self._write_conn.execute("ROLLBACK")
                        raise
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= 4:
                    raise WriteConflictError(str(exc)) from exc
                time.sleep(0.05 * (2**attempt))
        raise WriteConflictError("database is locked")

    # ── 设置 ────────────────────────────────────────────────

    def seed_settings(self, defaults: dict[str, Any]) -> None:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO system_settings (key, value_json, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )

        self._write(run)

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._read_conn.execute(
                "SELECT key, value_json FROM system_settings"
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[str(row["key"])] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                continue
        return result

    def update_settings(self, values: dict[str, Any], admin_id: int) -> None:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO system_settings (key, value_json, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value_json = excluded.value_json, updated_by = excluded.updated_by, "
                    "updated_at = excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), admin_id, now),
                )

        self._write(run)

    # ── Bark 推送 ──────────────────────────────────────────

    def get_user_bark(self, qq: int) -> sqlite3.Row | None:
        with self._lock:
            return self._read_conn.execute(
                "SELECT * FROM user_bark WHERE qq = ?", (int(qq),)
            ).fetchone()

    def save_user_bark(
        self,
        qq: int,
        *,
        server: str,
        device_key_enc: bytes,
        sound: str | None = None,
        enabled: bool = True,
    ) -> None:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
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
                    int(qq), server, device_key_enc, (sound or ""),
                    int(enabled), now, now, (sound or ""), (sound or ""),
                ),
            )

        self._write(run)

    def set_user_bark_sound(self, qq: int, sound: str) -> bool:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE user_bark SET sound = ?, updated_at = ? WHERE qq = ?",
                (sound, now, int(qq)),
            )
            return cur.rowcount > 0

        return bool(self._write(run))

    def set_user_bark_enabled(self, qq: int, enabled: bool) -> bool:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE user_bark SET enabled = ?, updated_at = ? WHERE qq = ?",
                (int(enabled), now, int(qq)),
            )
            return cur.rowcount > 0

        return bool(self._write(run))

    def delete_user_bark(self, qq: int) -> bool:
        def run(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM user_bark WHERE qq = ?", (int(qq),))
            return cur.rowcount > 0

        return bool(self._write(run))

    # ── 用户与权限 ──────────────────────────────────────────

    def list_users(self, *, query: str = "", limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        sql = """
            SELECT u.qq, u.created_at,
                   (SELECT COUNT(*) FROM subscriptions AS s WHERE s.qq = u.qq) AS sub_count,
                   (SELECT COUNT(*) FROM subscriptions AS s
                     WHERE s.qq = u.qq AND s.enabled = 1) AS active_sub_count,
                   t.status AS token_status, t.method AS token_method,
                   t.updated_at AS token_updated_at,
                   b.reason AS block_reason, b.blocked_until,
                   CASE WHEN b.qq IS NULL THEN 0
                        WHEN b.blocked_until IS NULL OR b.blocked_until > ? THEN 1
                        ELSE 0 END AS blocked,
                   p.max_subscriptions,
                   COALESCE(p.allow_subscribe, 1) AS allow_subscribe,
                   COALESCE(p.allow_login, 1) AS allow_login,
                   COALESCE(p.allow_trade, 1) AS allow_trade,
                   COALESCE(p.allow_price_query, 1) AS allow_price_query,
                   p.note,
                   bump.enabled AS bark_enabled,
                   bump.sound AS bark_sound,
                   CASE WHEN bump.qq IS NULL THEN 0 ELSE 1 END AS bark_configured
            FROM users AS u
            LEFT JOIN tokens AS t ON t.qq = u.qq
            LEFT JOIN blocked_users AS b ON b.qq = u.qq
            LEFT JOIN user_permissions AS p ON p.qq = u.qq
            LEFT JOIN user_bark AS bump ON bump.qq = u.qq
        """
        args: list[Any] = [int(time.time())]
        if query:
            sql += " WHERE CAST(u.qq AS TEXT) LIKE ?"
            args.append(f"%{query.strip()}%")
        sql += " ORDER BY u.qq LIMIT ? OFFSET ?"
        args.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            return self._read_conn.execute(sql, args).fetchall()

    def get_user(self, qq: int) -> sqlite3.Row | None:
        rows = self.list_users(query=str(qq), limit=200, offset=0)
        for row in rows:
            if int(row["qq"]) == int(qq):
                return row
        return None

    def get_permissions(self, qq: int) -> sqlite3.Row | None:
        with self._lock:
            return self._read_conn.execute(
                "SELECT * FROM user_permissions WHERE qq = ?", (qq,)
            ).fetchone()

    def update_permissions(self, qq: int, fields: dict[str, Any], admin_id: int) -> None:
        allowed = {
            "max_subscriptions", "allow_subscribe", "allow_login",
            "allow_trade", "allow_price_query", "note",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO user_permissions "
                "(qq, max_subscriptions, allow_subscribe, allow_login, allow_trade, "
                "allow_price_query, note, updated_by, updated_at) "
                "VALUES (?, NULL, 1, 1, 1, 1, '', ?, ?)",
                (qq, admin_id, now),
            )
            if not updates:
                conn.execute(
                    "UPDATE user_permissions SET updated_by = ?, updated_at = ? WHERE qq = ?",
                    (admin_id, now, qq),
                )
                return
            sets = ", ".join(f"{key} = ?" for key in updates)
            args = [int(v) if isinstance(v, bool) else v for v in updates.values()]
            conn.execute(
                f"UPDATE user_permissions SET {sets}, updated_by = ?, updated_at = ? WHERE qq = ?",
                (*args, admin_id, now, qq),
            )

        self._write(run)

    def reset_permissions(self, qq: int) -> int:
        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute("DELETE FROM user_permissions WHERE qq = ?", (qq,))
            return int(cur.rowcount)

        return int(self._write(run))

    def block_user(
        self, qq: int, *, reason: str = "", until: int | None = None, admin_id: int = 0
    ) -> None:
        now = int(time.time())

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO blocked_users (qq, reason, blocked_until, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(qq) DO UPDATE SET "
                "reason = excluded.reason, blocked_until = excluded.blocked_until, "
                "created_by = excluded.created_by, created_at = excluded.created_at",
                (qq, reason.strip(), until, admin_id, now),
            )

        self._write(run)

    def unblock_user(self, qq: int) -> int:
        def run(conn: sqlite3.Connection) -> int:
            cur = conn.execute("DELETE FROM blocked_users WHERE qq = ?", (qq,))
            return int(cur.rowcount)

        return int(self._write(run))

    # ── 订阅 ────────────────────────────────────────────────

    def list_subscriptions(
        self,
        *,
        qq: int | None = None,
        enabled: int | None = None,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM subscriptions"
        where: list[str] = []
        args: list[Any] = []
        if qq is not None:
            where.append("qq = ?")
            args.append(qq)
        if enabled is not None:
            where.append("enabled = ?")
            args.append(1 if enabled else 0)
        if query:
            where.append("(spec_json LIKE ? OR CAST(qq AS TEXT) LIKE ?)")
            args.extend([f"%{query.strip()}%", f"%{query.strip()}%"])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            return self._read_conn.execute(sql, args).fetchall()

    def get_subscription(self, sub_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._read_conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
            ).fetchone()

    def update_subscription(self, sub_id: int, fields: dict[str, Any]) -> bool:
        allowed = {
            "enabled", "interval_sec", "max_push", "only_new",
            "quiet_hours", "first_push", "next_run_at",
            "bark_enabled", "bark_sound", "flip_overrides_json",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        def run(conn: sqlite3.Connection) -> bool:
            sets = ", ".join(f"{key} = ?" for key in updates)
            args = [int(v) if isinstance(v, bool) else v for v in updates.values()]
            cur = conn.execute(
                f"UPDATE subscriptions SET {sets} WHERE id = ?", (*args, sub_id)
            )
            return cur.rowcount > 0

        return bool(self._write(run))

    def delete_subscription(self, sub_id: int) -> bool:
        def run(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            if cur.rowcount <= 0:
                return False
            conn.execute("DELETE FROM seen WHERE sub_id = ?", (sub_id,))
            return True

        return bool(self._write(run))

    # ── 分析数据（只读）────────────────────────────────────

    def list_disappeared(
        self,
        *,
        item_id: str | None = None,
        identity_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT id, item_key, item_type, item_id, item_name, sub_id, qq, "
            "identity_key, identity_json, first_seen_at, last_seen_at, disappeared_at, "
            "price, seller, seller_key, dt_register, quality, spec_json "
            "FROM disappeared_history"
        )
        where: list[str] = []
        args: list[Any] = []
        if item_id:
            where.append("item_id = ?")
            args.append(item_id)
        if identity_key:
            where.append("identity_key = ?")
            args.append(identity_key)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            return self._read_conn.execute(sql, args).fetchall()

    def list_identity_groups(
        self,
        *,
        item_id: str | None = None,
        identity_key: str | None = None,
        min_sellers: int = 2,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM v_identity_groups"
        where: list[str] = []
        args: list[Any] = []
        if item_id:
            where.append("item_key = ?")
            args.append(item_id)
        if identity_key:
            where.append("identity_key = ?")
            args.append(identity_key)
        if min_sellers and min_sellers > 0:
            where.append("sellers >= ?")
            args.append(int(min_sellers))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY listings DESC, last_disappeared_at DESC LIMIT ? OFFSET ?"
        args.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            return self._read_conn.execute(sql, args).fetchall()

    def get_identity_group(
        self, item_id: str, identity_key: str
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._read_conn.execute(
                "SELECT * FROM v_identity_groups "
                "WHERE item_key = ? AND identity_key = ?",
                (item_id, identity_key),
            ).fetchone()

    def get_identity_attributes(
        self, item_id: str, identity_key: str
    ) -> dict[str, Any]:
        """从最近的快照里还原品质、能力与辅助词条，用于详情展示。"""
        payload: dict[str, Any] = {}
        with self._lock:
            rows = []
            for table in ("sub_listings", "disappeared_history"):
                row = self._read_conn.execute(
                    f"SELECT identity_json FROM {table} "
                    "WHERE item_id = ? AND identity_key = ? AND identity_json <> '' "
                    "ORDER BY id DESC LIMIT 1",
                    (item_id, identity_key),
                ).fetchone()
                if row is not None:
                    rows.append(row[0])
        for text in rows:
            try:
                payload = json.loads(str(text))
                break
            except (TypeError, json.JSONDecodeError):
                continue
        return {
            "quality": payload.get("quality"),
            "abilities": payload.get("abilities") or {},
            "subperks": payload.get("subperks") or [],
        }

    def list_identity_history(
        self, item_id: str, identity_key: str, limit: int = 100
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._read_conn.execute(
                "SELECT id, seller_last AS seller, seller_key, price_first, price_last, "
                "first_seen_at, last_seen_at, disappeared_at, state "
                "FROM sub_listings WHERE item_id = ? AND identity_key = ? "
                "ORDER BY first_seen_at, id LIMIT ?",
                (item_id, identity_key, max(1, min(limit, 200))),
            ).fetchall()

    def list_group_sellers(
        self, item_id: str, identity_key: str
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._read_conn.execute(
                "SELECT * FROM item_sellers WHERE item_id = ? AND identity_key = ? "
                "ORDER BY is_original DESC, first_seen_at, seller_key",
                (item_id, identity_key),
            ).fetchall()

    def list_seller_flips(
        self,
        *,
        item_id: str | None = None,
        seller: str | None = None,
        identity_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM seller_flips"
        where: list[str] = []
        args: list[Any] = []
        if item_id:
            where.append("item_id = ?")
            args.append(item_id)
        if identity_key:
            where.append("identity_key = ?")
            args.append(identity_key)
        if seller:
            where.append("(reseller_key = ? OR reseller LIKE ?)")
            args.extend([seller, f"%{seller}%"])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY reseller_first_seen_at DESC, id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._lock:
            return self._read_conn.execute(sql, args).fetchall()

    # ── 状态 ────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        now = int(time.time())
        with self._lock:
            def exists(name: str) -> bool:
                row = self._read_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (name,),
                ).fetchone()
                return row is not None

            def one(sql: str, args: tuple[Any, ...] = (), *, table: str = "") -> Any:
                if table and not exists(table):
                    return 0
                row = self._read_conn.execute(sql, args).fetchone()
                return row[0] if row else 0

            return {
                "users": one("SELECT COUNT(*) FROM users", table="users"),
                "subscriptions": one(
                    "SELECT COUNT(*) FROM subscriptions", table="subscriptions"
                ),
                "active_subscriptions": one(
                    "SELECT COUNT(*) FROM subscriptions WHERE enabled = 1",
                    table="subscriptions",
                ),
                "blocked_users": one(
                    "SELECT COUNT(*) FROM blocked_users "
                    "WHERE blocked_until IS NULL OR blocked_until > ?",
                    (now,),
                    table="blocked_users",
                ),
                "tokens": one("SELECT COUNT(*) FROM tokens", table="tokens"),
                "disappeared": one(
                    "SELECT COUNT(*) FROM disappeared_history",
                    table="disappeared_history",
                ),
                "flips": one(
                    "SELECT COUNT(*) FROM seller_flips", table="seller_flips"
                ),
                "sellers": one(
                    "SELECT COUNT(DISTINCT seller_key) FROM item_sellers",
                    table="item_sellers",
                ),
                "last_run_at": one(
                    "SELECT MAX(started_at) FROM runs", table="runs"
                ),
                "last_crawl_at": one(
                    "SELECT MAX(started_at) FROM crawl_runs", table="crawl_runs"
                ),
            }

    def close(self) -> None:
        with self._lock:
            self._read_conn.close()
            self._write_conn.close()
