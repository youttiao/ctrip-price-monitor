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
        INSERT INTO vendors (vendor_id, name, brand_company_name, licence_no, last_seen_at, sku_count)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(vendor_id) DO UPDATE SET
            name=COALESCE(excluded.name, vendors.name),
            brand_company_name=COALESCE(excluded.brand_company_name, vendors.brand_company_name),
            licence_no=COALESCE(excluded.licence_no, vendors.licence_no),
            last_seen_at=excluded.last_seen_at,
            sku_count=vendors.sku_count + 1
    """, (vendor_id, vendor_name, brand, licence, captured_at))


def process_round(conn, round_pk: int, raw_path: str, poi_viewid: int):
    raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    captured_at = raw.get("capturedAt") or datetime.now(timezone.utc).isoformat()

    parsed = parse_round(raw, poi_viewid)
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
            conn, sku["vendor_id"], sku.get("vendor_name"),
            sku.get("brand_company_name"), sku.get("licence_no"), captured_at,
        )

    # 2) sku_snapshot 批量插入
    rows = [(
        round_pk, sku["resource_id"], poi_viewid,
        sku["vendor_id"], sku["full_name"], sku.get("market_price"),
        sku["display_price"], sku.get("sale_count"),
        sku.get("shelf_type_id"), sku.get("shelf_type_name"),
        sku.get("ticket_group_id"), sku.get("shelf_group_id"),
        sku.get("package_type"), sku.get("refund_rule"),
        captured_at,
    ) for sku in parsed["skus"]]

    conn.executemany("""
        INSERT INTO sku_snapshot (
            round_id, resource_id, poi_viewid, primary_vendor_id,
            full_name, market_price, display_price, sale_count,
            shelf_type_id, shelf_type_name,
            ticket_group_id, shelf_group_id,
            package_type, refund_rule,
            captured_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)

    # 3) rank_history
    rh_rows = compute_rank_history(conn, round_pk, poi_viewid)
    if rh_rows:
        conn.executemany("""
            INSERT INTO rank_history (
                round_id, poi_viewid, shelf_type_id, vendor_id, resource_id,
                rank, display_price, lowest_price, gap, is_mine, captured_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, rh_rows)

    # 4) alerts
    alerts = detect_rank_alerts(conn, round_pk, parsed)
    inserted = 0
    if alerts:
        now = datetime.now(timezone.utc).isoformat()
        for a in alerts:
            conn.execute("""
                INSERT OR IGNORE INTO alerts (
                        captured_at, ts, type, severity, poi_viewid, poi_name,
                        shelf_type_id, shelf_type_name, sku_name, vendor_id,
                        payload, dedup_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                captured_at, now, a["type"], a["severity"],
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
                payload = {
                    "ts": now,
                    "type": a["type"],
                    "severity": a["severity"],
                    "poi": {"viewid": a["poi_viewid"], "name": a["poi_name"]},
                    "shelf": {"type_id": a["shelf_type_id"], "name": a["shelf_type_name"]},
                    "sku": a["sku_name"],
                    "vendor_id": a["vendor_id"],
                    **a["payload"],
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
                "UPDATE rounds SET status='error', error=? WHERE id=?",
                (str(e)[:500], r["id"])
            )
            conn.commit()


if __name__ == "__main__":
    main()