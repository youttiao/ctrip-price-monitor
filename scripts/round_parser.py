#!/usr/bin/env python3
"""Round parser：从 ingest 进来的 pending round 解析 → sku_snapshot + rank_history + alerts。

每分钟跑一次（systemd timer）。
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctrip_core.parse import parse_round, compute_rank_history  # noqa: E402
from ctrip_core.alerts import detect_rank_alerts  # noqa: E402
from web.notifier import send_webhook  # noqa: E402


def get_config(conn, key: str, default=None):
    import json as _json
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return _json.loads(row["value"])
    except Exception:
        return row["value"]


def update_vendor_stats(conn, vendor_id: int, vendor_name: str | None, brand: str | None, licence: str | None, captured_at: str):
    conn.execute("""
        INSERT INTO vendors (vendor_id, name, brand_company_name, licence_no,
                             first_seen_at, last_seen_at, sku_count)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(vendor_id) DO UPDATE SET
            name=COALESCE(excluded.name, vendors.name),
            brand_company_name=COALESCE(excluded.brand_company_name, vendors.brand_company_name),
            licence_no=COALESCE(excluded.licence_no, vendors.licence_no),
            last_seen_at=excluded.last_seen_at,
            sku_count=vendors.sku_count + 1
    """, (vendor_id, vendor_name, brand, licence, captured_at, captured_at))


def upsert_ticket_meta(conn, rows: list[tuple], captured_at: str, round_pk: int):
    """跨轮稳定的票种元数据 upsert。

    自然键 (poi_viewid, resource_id); vendor/shelf 等字段用 COALESCE 保留
    缺数据轮不覆盖。新 rid 直接 INSERT。

    rows 元素必须有 17 个字段 (不含时间戳): 见 process_round 里的 tm_rows 形态。
    这里再追加 (first_seen_at, last_seen_at, last_round_id, created_at, updated_at) 5 个。
    """
    if not rows:
        return 0
    now = captured_at
    enriched = [
        r + (now, now, round_pk, now, now) for r in rows
    ]
    conn.executemany("""
        INSERT INTO ticket_meta (
            poi_viewid, resource_id,
            full_name, primary_vendor_id, primary_vendor_name, primary_vendor_brand,
            primary_vendor_licence, primary_vendor_licence_pic,
            shelf_type_id, shelf_type_name, spotid,
            parent_resource_id, people_property,
            market_price, sale_count, first_booking_date,
            raw_resource,
            first_seen_at, last_seen_at, last_round_id,
            created_at, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(poi_viewid, resource_id) DO UPDATE SET
            full_name                 = COALESCE(NULLIF(excluded.full_name, ''),                 ticket_meta.full_name),
            primary_vendor_id         = CASE WHEN excluded.primary_vendor_id > 0
                                              THEN excluded.primary_vendor_id
                                              ELSE ticket_meta.primary_vendor_id END,
            primary_vendor_name       = COALESCE(NULLIF(excluded.primary_vendor_name, ''),       ticket_meta.primary_vendor_name),
            primary_vendor_brand      = COALESCE(NULLIF(excluded.primary_vendor_brand, ''),      ticket_meta.primary_vendor_brand),
            primary_vendor_licence    = COALESCE(NULLIF(excluded.primary_vendor_licence, ''),    ticket_meta.primary_vendor_licence),
            primary_vendor_licence_pic= COALESCE(NULLIF(excluded.primary_vendor_licence_pic, ''),ticket_meta.primary_vendor_licence_pic),
            shelf_type_id             = COALESCE(excluded.shelf_type_id,              ticket_meta.shelf_type_id),
            shelf_type_name           = COALESCE(NULLIF(excluded.shelf_type_name, ''), ticket_meta.shelf_type_name),
            spotid                    = COALESCE(excluded.spotid,                     ticket_meta.spotid),
            parent_resource_id        = COALESCE(excluded.parent_resource_id,         ticket_meta.parent_resource_id),
            people_property           = COALESCE(NULLIF(excluded.people_property, ''),ticket_meta.people_property),
            market_price              = COALESCE(excluded.market_price,               ticket_meta.market_price),
            sale_count                = COALESCE(excluded.sale_count,                 ticket_meta.sale_count),
            first_booking_date        = COALESCE(NULLIF(excluded.first_booking_date,''),ticket_meta.first_booking_date),
            raw_resource              = excluded.raw_resource,
            last_seen_at              = excluded.last_seen_at,
            last_round_id             = excluded.last_round_id,
            updated_at                = excluded.updated_at
    """, enriched)
    return len(enriched)


def process_round(conn, round_pk: int, raw_path: str, poi_viewid: int):
    raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    captured_at = raw.get("capturedAt") or datetime.now(timezone.utc).isoformat()

    parsed = parse_round(raw)
    if not parsed["skus"]:
        conn.execute(
            "UPDATE rounds SET status='empty', parsed_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), round_pk)
        )
        conn.commit()
        return 0

    # 1) vendor stats
    for sku in parsed["skus"]:
        update_vendor_stats(
            conn, sku["primary_vendor_id"], sku.get("primary_vendor_name"),
            sku.get("primary_vendor_brand"), sku.get("primary_vendor_licence"), captured_at,
        )

    # 1.5) ticket_meta 跨轮稳定元数据 upsert (按 (poi_viewid, resource_id) 自然键)
    #      让 dashboard "票种不丢": 本轮没抓到的 rid 仍能在 ticket_meta 里占位
    #      直到 retention 把它清除 (7 天未抓到)。
    tm_rows = [(
        poi_viewid, sku["resource_id"],
        sku.get("full_name"), sku["primary_vendor_id"], sku.get("primary_vendor_name"),
        sku.get("primary_vendor_brand"), sku.get("primary_vendor_licence"),
        sku.get("primary_vendor_licence_pic"),
        sku.get("shelf_type_id"), sku.get("shelf_type_name"), sku.get("spotid"),
        sku.get("parent_resource_id"), sku.get("people_property"),
        sku.get("market_price"), sku.get("sale_count"), sku.get("first_booking_date"),
        json.dumps(sku.get("raw_resource") or {}, ensure_ascii=False),
    ) for sku in parsed["skus"]]
    upsert_ticket_meta(conn, tm_rows, captured_at, round_pk)

    # 2) sku_snapshot 批量插入
    rows = [(
        round_pk, sku["resource_id"], poi_viewid,
        sku["primary_vendor_id"], sku["full_name"],
        sku.get("shelf_type_id"), sku.get("shelf_type_name"),
        sku.get("spotid"),
        sku.get("primary_vendor_name"), sku.get("primary_vendor_brand"),
        sku.get("primary_vendor_licence"), sku.get("primary_vendor_licence_pic"),
        sku["display_price"], sku.get("market_price"),
        sku.get("first_booking_date"), sku.get("sale_count"),
        sku.get("parent_resource_id"), sku.get("people_property"),
        json.dumps(sku.get("raw_resource") or {}, ensure_ascii=False),
    ) for sku in parsed["skus"]]

    conn.executemany("""
        INSERT OR IGNORE INTO sku_snapshot (
            round_id, resource_id, poi_viewid,
            primary_vendor_id, full_name,
            shelf_type_id, shelf_type_name, spotid,
            primary_vendor_name, primary_vendor_brand,
            primary_vendor_licence, primary_vendor_licence_pic,
            display_price, market_price,
            first_booking_date, sale_count,
            parent_resource_id, people_property,
            raw_resource
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)

    # 2.5) price_day 批量插入（每个 rid × 每个 sale_date × 每个 package_id 一行）
    pd_rows = [(
        round_pk, pd_row["resource_id"], poi_viewid,
        pd_row["sale_date"],
        pd_row["min_price"], pd_row["sale_price"],
        pd_row["inventory"], int(pd_row["available"]) if pd_row["available"] is not None else None,
        pd_row["package_id"],
        pd_row.get("winning_vendor_id"),
        pd_row.get("people_property"),
        json.dumps(pd_row.get("raw") or {}, ensure_ascii=False),
    ) for pd_row in parsed["price_days"]
        if pd_row.get("sale_date") and pd_row.get("resource_id")]

    if pd_rows:
        conn.executemany("""
            INSERT OR REPLACE INTO price_day (
                round_id, resource_id, poi_viewid,
                sale_date, min_price, sale_price,
                inventory, available, package_id, winning_vendor_id,
                people_property, raw
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, pd_rows)

    # 3) rank_history
    rh_rows = compute_rank_history(conn, round_pk, poi_viewid)
    if rh_rows:
        rh_tuples = [(
            r["round_id"], r["poi_viewid"], r["shelf_type_id"],
            r["vendor_id"], r["resource_id"], r["rank"],
            r["display_price"], r["lowest_resource_id"], r["lowest_price"],
            r["gap"], r["is_mine"], captured_at,
        ) for r in rh_rows]
        conn.executemany("""
            INSERT OR IGNORE INTO rank_history (
                round_id, poi_viewid, shelf_type_id, vendor_id, resource_id,
                rank, display_price, lowest_resource_id, lowest_price,
                gap, is_mine, captured_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, rh_tuples)

    # 4) alerts
    alerts = detect_rank_alerts(conn, round_pk, parsed)
    inserted = 0
    if alerts:
        now = datetime.now(timezone.utc).isoformat()
        for a in alerts:
            conn.execute("""
                INSERT OR IGNORE INTO alerts (
                        captured_at, ts, round_id, resource_id,
                        type, severity, poi_viewid, poi_name,
                        shelf_type_id, shelf_type_name, sku_name, vendor_id,
                        payload, dedup_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                captured_at, now, round_pk, a.get("resource_id"),
                a["type"], a["severity"],
                a["poi_viewid"], a["poi_name"],
                a["shelf_type_id"], a["shelf_type_name"],
                a["sku_name"], a["vendor_id"],
                json.dumps(a["payload"], ensure_ascii=False),
                a["dedup_key"],
            ))
        conn.commit()
        inserted = len(alerts)

        # 5) webhook
        webhook_url = get_config(conn, "webhook_url")
        webhook_secret = get_config(conn, "webhook_secret")
        if webhook_url:
            for a in alerts:
                # payload 存的是 JSON 字符串，webhook splat 前要 parse 回 dict
                payload_obj = a["payload"]
                if isinstance(payload_obj, str):
                    try:
                        payload_obj = json.loads(payload_obj)
                    except Exception:
                        payload_obj = {}
                payload = {
                    "ts": now,
                    "type": a["type"],
                    "severity": a["severity"],
                    "poi": {"viewid": a["poi_viewid"], "name": a["poi_name"]},
                    "shelf": {"type_id": a["shelf_type_id"], "name": a["shelf_type_name"]},
                    "sku": a["sku_name"],
                    "vendor_id": a["vendor_id"],
                    **payload_obj,
                }
                ok, err = send_webhook(webhook_url, webhook_secret, payload, a["dedup_key"])
                conn.execute(
                    "UPDATE alerts SET webhook_status=? WHERE dedup_key=?",
                    ("ok" if ok else f"fail:{err}", a["dedup_key"])
                )

    # 6) round status
    conn.execute("""
        UPDATE rounds SET status='parsed', parsed_at=?,
                          sku_count=?, alert_count=?
        WHERE id=?
    """, (datetime.now(timezone.utc).isoformat(),
          len(parsed["skus"]), inserted, round_pk))

    # 6.5) pois.last_round_id + last_status → admin UI reflects reality
    conn.execute("""
        UPDATE pois SET last_round_id=?, last_status=?, last_error=NULL, updated_at=?
        WHERE viewid=?
    """, (round_pk, "parsed",
          datetime.now(timezone.utc).isoformat(), poi_viewid))

    conn.commit()
    return len(parsed["skus"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.getenv("CTRIP_DB_PATH", "data/monitor.db"))
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    import sqlite3
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    pending = conn.execute("""
        SELECT id, raw_path, poi_viewid FROM rounds
        WHERE status='pending'
        ORDER BY id ASC LIMIT ?
    """, (args.limit,)).fetchall()

    if not pending:
        print("no pending rounds")
        return

    for r in pending:
        try:
            n = process_round(conn, r["id"], r["raw_path"], r["poi_viewid"])
            print(f"round {r['id']}: parsed {n} SKUs")
        except Exception as e:
            print(f"round {r['id']}: ERROR {e}", file=sys.stderr)
            conn.execute(
                "UPDATE rounds SET status='error', error_msg=? WHERE id=?",
                (str(e)[:500], r["id"])
            )
            conn.execute("""
                UPDATE pois SET last_status=?, last_error=?, updated_at=?
                WHERE viewid=?
            """, ("error", str(e)[:500],
                  datetime.now(timezone.utc).isoformat(), r["poi_viewid"]))
            conn.commit()


if __name__ == "__main__":
    main()