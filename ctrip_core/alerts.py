"""vendor-centric 告警引擎。

核心数据单位：(poi_viewid, shelf_type, vendorId)
我的集合：从 my_vendors 表读。
告警类型：
- rank_drop      → 我从 rank N1 掉到 rank N2 (N2 > N1)
- rank_up        → 我从 rank N1 升到 rank N2 (N2 < N1)
- appeared       → 我新出现在某 shelfType（前一轮不在）
- disappeared    → 我从某 shelfType 消失（前一轮在，现在不在）→ critical
- still_non_first → 我持续非 #1（每轮评估一次，dedup_key 唯一）

设计原则：只对 watchlist 的 shelfType 评估，避免噪声。
"""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone


def detect_rank_alerts(conn, round_pk: int, parsed: dict) -> list[dict]:
    """对每个 watchlist 的 shelfType 比较本轮 rank vs 上一轮。

    conn: sqlite3 connection（要求 row_factory=Row）
    round_pk: 当前 round 的主键
    parsed: parse_round 的输出
    """
    viewid = parsed["viewid"]

    # 1. 我的 vendorIds
    my_vids = {r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1").fetchall()}
    if not my_vids:
        return []

    # 2. 我关注的 shelfType（包含 watchlist 中的 + 当前 round 涉及到的）
    watched_rows = conn.execute("""
        SELECT DISTINCT shelf_type_id FROM watchlist
        WHERE user_id=(SELECT MIN(id) FROM users WHERE is_active=1)
    """).fetchall()
    watched_shelves = {r["shelf_type_id"] for r in watched_rows}

    # 3. 本轮我的 rank（从 rank_history 表读，因为写入顺序：先 SKU → 再 rank_history）
    my_now_rows = conn.execute("""
        SELECT rh.shelf_type_id, rh.rank, rh.display_price, rh.lowest_price, rh.gap,
               rh.resource_id, rh.vendor_id,
               s.shelf_type_name, s.full_name
        FROM rank_history rh
        JOIN sku_snapshot s ON s.round_id=rh.round_id AND s.resource_id=rh.resource_id
        WHERE rh.round_id=? AND rh.is_mine=1
    """, (round_pk,)).fetchall()
    my_now = {r["shelf_type_id"]: r for r in my_now_rows}

    # 4. 我上一轮的 rank（最近一个已解析 round）
    prev_round_row = conn.execute("""
        SELECT MAX(id) FROM rounds
        WHERE poi_viewid=? AND status='parsed' AND id<?
    """, (viewid, round_pk)).fetchone()
    prev_round_pk = prev_round_row[0] if prev_round_row else None

    my_prev = {}
    if prev_round_pk:
        prev_rows = conn.execute("""
            SELECT rh.shelf_type_id, rh.rank, rh.display_price, rh.lowest_price, rh.gap,
                   rh.resource_id, rh.vendor_id,
                   s.shelf_type_name, s.full_name
            FROM rank_history rh
            JOIN sku_snapshot s ON s.round_id=rh.round_id AND s.resource_id=rh.resource_id
            WHERE rh.round_id=? AND rh.is_mine=1
        """, (prev_round_pk,)).fetchall()
        my_prev = {r["shelf_type_id"]: r for r in prev_rows}

    # 5. 评估
    alerts = []
    seen_shelves = set(my_now) | set(my_prev)
    for shelf_id in seen_shelves:
        # 仅对 watchlist 中的 shelfType 发告警；watchlist 完全为空时静默
        if not watched_shelves or shelf_id not in watched_shelves:
            continue

        now = my_now.get(shelf_id)
        prev = my_prev.get(shelf_id)
        sku_name = (now or prev)["full_name"] if (now or prev) else None
        shelf_name = (now or prev)["shelf_type_name"] if (now or prev) else None
        vendor_id = (now or prev)["vendor_id"] if (now or prev) else None

        if now and not prev:
            alerts.append(_mk_alert(
                round_pk, "appeared", "info",
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"new_rank": now["rank"], "display_price": now["display_price"],
                 "lowest_price": now["lowest_price"], "gap": now["gap"]},
            ))
        elif prev and not now:
            alerts.append(_mk_alert(
                round_pk, "disappeared", "critical",
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"was_rank": prev["rank"], "was_price": prev["display_price"]},
            ))
        elif now and prev and now["rank"] != prev["rank"]:
            kind = "rank_drop" if now["rank"] > prev["rank"] else "rank_up"
            sev = "warning" if kind == "rank_drop" else "info"
            alerts.append(_mk_alert(
                round_pk, kind, sev,
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"old_rank": prev["rank"], "new_rank": now["rank"],
                 "my_price": now["display_price"],
                 "lowest_price": now["lowest_price"], "gap": now["gap"]},
            ))
        elif now and prev and now["rank"] == prev["rank"] and now["rank"] != 1:
            # 持续非 #1 状态：评估每轮，但 dedup_key 含 captured_at 不重不漏
            alerts.append(_mk_alert(
                round_pk, "still_non_first", "warning",
                viewid, parsed, shelf_id, shelf_name, sku_name, vendor_id,
                {"rank": now["rank"], "my_price": now["display_price"],
                 "lowest_price": now["lowest_price"], "gap": now["gap"]},
            ))

    return alerts


def _mk_alert(round_pk, kind, sev, viewid, parsed,
              shelf_id, shelf_name, sku_name, vendor_id, payload_dict):
    """构造一条 alert 字典（未入库）。"""
    ts = datetime.now(timezone.utc).isoformat()
    # dedup_key 含 captured_at 保证 still_non_first 每轮唯一
    rank_part = payload_dict.get("new_rank", payload_dict.get("rank", ""))
    dedup = hashlib.sha1(
        f"{kind}|{viewid}|{shelf_id}|{vendor_id}|{rank_part}|{parsed['captured_at']}".encode()
    ).hexdigest()[:24]
    return {
        "ts": ts, "round_id": round_pk, "type": kind, "severity": sev,
        "poi_viewid": viewid, "poi_name": parsed.get("poi_name"),
        "shelf_type_id": shelf_id, "shelf_type_name": shelf_name,
        "resource_id": None, "sku_name": sku_name,
        "vendor_id": vendor_id, "payload": json.dumps(payload_dict, ensure_ascii=False),
        "dedup_key": dedup,
    }


def insert_alerts(conn, alerts: list[dict]):
    """批量写 alerts 表（IGNORE 重复 dedup_key）。"""
    if not alerts:
        return
    for a in alerts:
        conn.execute("""
            INSERT OR IGNORE INTO alerts (ts, round_id, type, severity, poi_viewid, poi_name,
                shelf_type_id, shelf_type_name, resource_id, sku_name, vendor_id,
                payload, dedup_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (a["ts"], a["round_id"], a["type"], a["severity"],
              a["poi_viewid"], a["poi_name"], a["shelf_type_id"],
              a["shelf_type_name"], a["resource_id"], a["sku_name"],
              a["vendor_id"], a["payload"], a["dedup_key"]))
    conn.commit()