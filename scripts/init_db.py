"""初始化 SQLite DB：建表 + 默认配置。"""
from __future__ import annotations
import argparse, os, secrets, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        TEXT UNIQUE NOT NULL,
    captured_at     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    parsed_at       TEXT,
    poi_viewid      INTEGER NOT NULL,
    poi_name        TEXT,
    source          TEXT NOT NULL,
    requests_count  INTEGER DEFAULT 0,
    sku_count       INTEGER DEFAULT 0,
    alert_count     INTEGER DEFAULT 0,
    skus_mine       INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    error_msg       TEXT,
    duration_ms     INTEGER,
    raw_path        TEXT
);
CREATE INDEX IF NOT EXISTS idx_rounds_viewid ON rounds(poi_viewid);
CREATE INDEX IF NOT EXISTS idx_rounds_received ON rounds(received_at DESC);

CREATE TABLE IF NOT EXISTS sku_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    resource_id     INTEGER NOT NULL,
    primary_vendor_id INTEGER NOT NULL,
    full_name       TEXT NOT NULL,
    shelf_type_id   INTEGER,
    shelf_type_name TEXT,
    spotid          INTEGER,
    primary_vendor_name  TEXT,
    primary_vendor_brand TEXT,
    primary_vendor_licence TEXT,
    primary_vendor_licence_pic TEXT,
    display_price   REAL,
    market_price    REAL,
    first_booking_date TEXT,
    sale_count      INTEGER,
    raw_resource    TEXT,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_sku_round ON sku_snapshot(round_id);
CREATE INDEX IF NOT EXISTS idx_sku_viewid ON sku_snapshot(poi_viewid);
CREATE INDEX IF NOT EXISTS idx_sku_vendor ON sku_snapshot(primary_vendor_id);
CREATE INDEX IF NOT EXISTS idx_sku_shelf ON sku_snapshot(shelf_type_id);

CREATE TABLE IF NOT EXISTS price_day (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    resource_id     INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    sale_date       TEXT NOT NULL,
    min_price       REAL,
    sale_price      REAL,
    inventory       INTEGER,
    available       INTEGER,
    package_id      INTEGER,
    raw             TEXT,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, resource_id, sale_date, package_id)
);
CREATE INDEX IF NOT EXISTS idx_pd_viewid ON price_day(poi_viewid);
CREATE INDEX IF NOT EXISTS idx_pd_date ON price_day(sale_date);
CREATE INDEX IF NOT EXISTS idx_pd_resid ON price_day(resource_id);
CREATE INDEX IF NOT EXISTS idx_pd_pkg ON price_day(package_id);

CREATE TABLE IF NOT EXISTS vendors (
    vendor_id       INTEGER PRIMARY KEY,
    name            TEXT,
    brand_company_name TEXT,
    licence_no      TEXT,
    licence_pic_url TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    sku_count       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vendors_brand ON vendors(brand_company_name);

CREATE TABLE IF NOT EXISTS my_vendors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id       INTEGER NOT NULL UNIQUE,
    label           TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_my_vendors_active ON my_vendors(is_active);

CREATE TABLE IF NOT EXISTS pois (
    viewid          INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    district        TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_round_id   INTEGER,
    last_status     TEXT,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    shelf_type_id   INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(user_id, poi_viewid, shelf_type_id)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);

CREATE TABLE IF NOT EXISTS rank_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id        INTEGER NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    shelf_type_id   INTEGER NOT NULL,
    vendor_id       INTEGER NOT NULL,
    resource_id     INTEGER NOT NULL,
    rank            INTEGER NOT NULL,
    display_price   REAL NOT NULL,
    lowest_resource_id INTEGER,
    lowest_price    REAL,
    gap             REAL,
    is_mine         INTEGER NOT NULL DEFAULT 0,
    captured_at     TEXT,
    FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
    UNIQUE(round_id, shelf_type_id, vendor_id)
);
CREATE INDEX IF NOT EXISTS idx_rh_round ON rank_history(round_id);
CREATE INDEX IF NOT EXISTS idx_rh_shelf ON rank_history(poi_viewid, shelf_type_id);
CREATE INDEX IF NOT EXISTS idx_rh_vendor ON rank_history(vendor_id);
CREATE INDEX IF NOT EXISTS idx_rh_mine ON rank_history(is_mine);

CREATE TABLE IF NOT EXISTS daily_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date    TEXT NOT NULL,
    poi_viewid      INTEGER NOT NULL,
    shelf_type_id   INTEGER NOT NULL,
    vendor_id       INTEGER NOT NULL,
    rank_min        INTEGER,
    rank_max        INTEGER,
    rank_avg        REAL,
    price_min       REAL,
    price_max       REAL,
    price_avg       REAL,
    rounds_count    INTEGER NOT NULL,
    UNIQUE(summary_date, poi_viewid, shelf_type_id, vendor_id)
);
CREATE INDEX IF NOT EXISTS idx_ds_date ON daily_summary(summary_date);

CREATE TABLE IF NOT EXISTS cookies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    blob_json       TEXT NOT NULL,
    uploaded_at     TEXT NOT NULL,
    source          TEXT NOT NULL,
    uploaded_by     TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    pw_hash         TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    sid             TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    ip_prefix       TEXT NOT NULL,
    user_agent      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at     TEXT,
    ts              TEXT NOT NULL,
    round_id        INTEGER,
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    poi_viewid      INTEGER,
    poi_name        TEXT,
    shelf_type_id   INTEGER,
    shelf_type_name TEXT,
    resource_id     INTEGER,
    sku_name        TEXT,
    vendor_id       INTEGER,
    payload         TEXT NOT NULL,
    dedup_key       TEXT NOT NULL UNIQUE,
    webhook_status  TEXT,
    webhook_sent_at TEXT,
    webhook_resp    TEXT,
    webhook_retry   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(type);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);

CREATE TABLE IF NOT EXISTS config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/monitor.db")
    parser.add_argument("--admin-password",
                        help="默认 admin 密码（否则用 env CTRIP_ADMIN_PASSWORD）")
    parser.add_argument("--api-secret",
                        help="默认 API secret（否则自动生成）")
    parser.add_argument("--reset", action="store_true",
                        help="删除旧 DB 后重建")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()

    now = datetime.now(timezone.utc).isoformat()

    # 默认 config
    api_secret = args.api_secret or os.getenv("API_SECRET") or secrets.token_urlsafe(32)
    admin_pwd = args.admin_password or os.getenv("CTRIP_ADMIN_PASSWORD") or secrets.token_urlsafe(12)

    import bcrypt
    pw_hash = bcrypt.hashpw(admin_pwd.encode(), bcrypt.gensalt()).decode()

    defaults = [
        ("api_secret", f'"{api_secret}"'),
        ("webhook_url", "null"),
        ("webhook_secret", "null"),
        ("alert_threshold_rank_drop", "1"),
        ("alert_threshold_ext_offline_min", "120"),
        ("site_name", '"携程哨兵"'),
    ]
    for k, v in defaults:
        conn.execute("""
            INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO NOTHING
        """, (k, v, now))

    # 默认 admin user
    conn.execute("""
        INSERT INTO users (username, pw_hash, role, created_at)
        VALUES (?, ?, 'admin', ?)
        ON CONFLICT(username) DO NOTHING
    """, ("admin", pw_hash, now))

    # 默认 5 POI
    default_pois = [
        (233, "天坛公园"),
        (5170, "景山公园"),
        (5153, "雍和宫"),
        (231, "颐和园"),
        (5208, "圆明园"),
    ]
    for v, n in default_pois:
        conn.execute("""
            INSERT INTO pois (viewid, name, created_at, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(viewid) DO NOTHING
        """, (v, n, now, now))

    conn.commit()
    conn.close()

    print(f"✓ DB initialized: {db_path}")
    print(f"  api_secret = {api_secret}")
    print(f"  admin password = {admin_pwd}")
    print("  (record these in /etc/ctrip-monitor/secrets.env)")


if __name__ == "__main__":
    main()