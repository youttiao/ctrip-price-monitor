"""告警引擎单测：模拟两轮 round → 验证 rank_drop / appeared / disappeared 告警。"""
import sqlite3
import pytest

from ctrip_core.alerts import detect_rank_alerts, insert_alerts


@pytest.fixture
def conn():
    """内存 DB，schema 来自 DESIGN.md。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE my_vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id INTEGER NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            shelf_type_id INTEGER NOT NULL
        );
        CREATE TABLE rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id TEXT, captured_at TEXT, received_at TEXT,
            poi_viewid INTEGER, source TEXT, status TEXT
        );
        CREATE TABLE sku_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            resource_id INTEGER NOT NULL,
            primary_vendor_id INTEGER NOT NULL,
            display_price REAL,
            shelf_type_id INTEGER,
            shelf_type_name TEXT,
            full_name TEXT
        );
        CREATE TABLE rank_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            shelf_type_id INTEGER NOT NULL,
            vendor_id INTEGER NOT NULL,
            resource_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            display_price REAL NOT NULL,
            lowest_resource_id INTEGER, lowest_price REAL,
            gap REAL, is_mine INTEGER NOT NULL DEFAULT 0,
            shelf_type_name TEXT, full_name TEXT
        );
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, round_id INTEGER, type TEXT, severity TEXT,
            poi_viewid INTEGER, poi_name TEXT,
            shelf_type_id INTEGER, shelf_type_name TEXT,
            resource_id INTEGER, sku_name TEXT, vendor_id INTEGER,
            payload TEXT, dedup_key TEXT UNIQUE
        );
    """)
    # 1 个 user
    c.execute("INSERT INTO users (id, username) VALUES (1, 'admin')")
    # 我的 vendorIds = {1184705, 999999}
    c.execute("INSERT INTO my_vendors (vendor_id) VALUES (1184705)")
    c.execute("INSERT INTO my_vendors (vendor_id) VALUES (999999)")
    # watchlist 里关注 shelf_type=791885 和 791887
    c.execute("INSERT INTO watchlist (user_id, shelf_type_id) VALUES (1, 791885)")
    c.execute("INSERT INTO watchlist (user_id, shelf_type_id) VALUES (1, 791887)")
    c.commit()
    return c


def _seed_round(conn, round_pk, viewid, captured_at, shelves, status="parsed"):
    """shelves = [{shelf_id, shelf_name, sku_list: [(rid, vendor_id, price, full_name)]}, ...]"""
    conn.execute("""
        INSERT INTO rounds (id, round_id, captured_at, received_at, poi_viewid, source, status)
        VALUES (?, ?, ?, ?, ?, 'extension', ?)
    """, (round_pk, f"r{round_pk}", captured_at, captured_at, viewid, status))

    my_vids = {1184705, 999999}

    for sh in shelves:
        sku_list = sh["sku_list"]
        sorted_skus = sorted(sku_list, key=lambda x: x[2])  # by price
        for rank_idx, (rid, vid, price, full_name) in enumerate(sorted_skus, start=1):
            is_mine = 1 if vid in my_vids else 0
            conn.execute("""
                INSERT INTO sku_snapshot
                  (round_id, resource_id, primary_vendor_id, display_price,
                   shelf_type_id, shelf_type_name, full_name)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (round_pk, rid, vid, price, sh["shelf_id"], sh["shelf_name"], full_name))
            lowest_price = sorted_skus[0][2]
            conn.execute("""
                INSERT INTO rank_history
                  (round_id, shelf_type_id, vendor_id, resource_id, rank,
                   display_price, lowest_resource_id, lowest_price, gap, is_mine,
                   shelf_type_name, full_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (round_pk, sh["shelf_id"], vid, rid, rank_idx, price,
                  sorted_skus[0][0], lowest_price,
                  round(price - lowest_price, 2), is_mine,
                  sh["shelf_name"], full_name))
    conn.commit()


def test_no_alerts_when_my_vids_empty(conn):
    """如果我没配 vendorIds → 不发任何告警。"""
    conn.execute("DELETE FROM my_vendors")
    conn.commit()
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 45, "我的SKU")],
    }])
    alerts = detect_rank_alerts(conn, 1, parsed)
    assert alerts == []


def test_rank_drop_warning(conn):
    """round 1 我 #1, round 2 我 #2 → 1 条 rank_drop warning。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 45, "我的SKU"), (2, 2852116, 50, "竞品A")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A"), (1, 1184705, 45, "我的SKU")],
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "rank_drop"
    assert alerts[0]["severity"] == "warning"
    import json
    payload = json.loads(alerts[0]["payload"])
    assert payload["old_rank"] == 1
    assert payload["new_rank"] == 2
    assert payload["gap"] == 3


def test_rank_up_info(conn):
    """round 1 我 #2, round 2 我 #1 → 1 条 rank_up info。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A"), (1, 1184705, 45, "我的SKU")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 41, "我的SKU"), (2, 2852116, 42, "竞品A")],
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "rank_up"
    assert alerts[0]["severity"] == "info"


def test_disappeared_critical(conn):
    """round 1 我在, round 2 我不在 shelfType → 1 条 disappeared critical。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 45, "我的SKU")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A")],  # 我消失了
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "disappeared"
    assert alerts[0]["severity"] == "critical"


def test_appeared_info(conn):
    """round 1 不在, round 2 出现 → 1 条 appeared info。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A")],  # 我不在
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 41, "我的SKU"), (2, 2852116, 42, "竞品A")],
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "appeared"
    assert alerts[0]["severity"] == "info"


def test_still_non_first_warning(conn):
    """连续两轮我都在 #2 → 第二轮触发 still_non_first warning。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A"), (1, 1184705, 45, "我的SKU")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A"), (1, 1184705, 45, "我的SKU")],
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "still_non_first"
    assert alerts[0]["severity"] == "warning"


def test_stable_first_silent(conn):
    """连续两轮我都在 #1 → 0 告警。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 41, "我的SKU"), (2, 2852116, 50, "竞品A")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 41, "我的SKU"), (2, 2852116, 50, "竞品A")],
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert alerts == []


def test_unwatched_shelf_silent(conn):
    """未加入 watchlist 的 shelfType → 不发告警。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 999999, "shelf_name": "未关注的货架",
        "sku_list": [(1, 1184705, 45, "我的SKU")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 999999, "shelf_name": "未关注的货架",
        "sku_list": [(1, 1184705, 45, "我的SKU")],
    }])

    alerts = detect_rank_alerts(conn, 2, parsed2)
    assert alerts == []


def test_insert_alerts_dedup(conn):
    """相同 dedup_key 的 alert 写入第二次应被忽略。"""
    parsed = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:00:00Z"}
    _seed_round(conn, 1, 233, "2026-08-24T10:00:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(1, 1184705, 45, "我的SKU"), (2, 2852116, 50, "竞品A")],
    }])
    parsed2 = {"viewid": 233, "poi_name": "天坛", "captured_at": "2026-08-24T10:30:00Z"}
    _seed_round(conn, 2, 233, "2026-08-24T10:30:00Z", [{
        "shelf_id": 791885, "shelf_name": "讲解服务",
        "sku_list": [(2, 2852116, 42, "竞品A"), (1, 1184705, 45, "我的SKU")],
    }])
    alerts = detect_rank_alerts(conn, 2, parsed2)
    insert_alerts(conn, alerts)
    insert_alerts(conn, alerts)  # 第二次应被 dedup

    count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    assert count == 1