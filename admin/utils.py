"""后台 API 通用工具。"""

from __future__ import annotations

import sqlite3
from typing import Any


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_dict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_dict(row) for row in rows if row is not None]
