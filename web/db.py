"""SQLite 连接管理：单 connection + WAL + row_factory。"""
from __future__ import annotations
import sqlite3


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_config(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    import json
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_config(conn, key: str, value):
    import json
    from datetime import datetime, timezone
    conn.execute("""
        INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (key, json.dumps(value, ensure_ascii=False),
          datetime.now(timezone.utc).isoformat()))
    conn.commit()