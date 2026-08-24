#!/usr/bin/env python3
"""Retention：每天 02:00 跑。

- 30 天前的 sku_snapshot / rounds / rank_history / alerts 删除
- raw_rounds/*.json 同样删除
- daily_summary 聚合（每 POI × shelfType × vendor × day → 最低/最高/平均/末位 rank）
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def aggregate_daily(conn, cutoff_day: str):
    """前一天聚合到 daily_summary。"""
    rows = conn.execute("""
        SELECT poi_viewid, shelf_type_id, primary_vendor_id,
               DATE(captured_at) AS day,
               MIN(display_price) AS min_price,
               MAX(display_price) AS max_price,
               AVG(display_price) AS avg_price,
               MIN(rank_history.rank) AS best_rank,
               MAX(rank_history.rank) AS worst_rank,
               COUNT(DISTINCT resource_id) AS sku_count
        FROM sku_snapshot
        LEFT JOIN rank_history ON rank_history.round_id=sku_snapshot.round_id
                              AND rank_history.resource_id=sku_snapshot.resource_id
        WHERE DATE(captured_at)=?
        GROUP BY poi_viewid, shelf_type_id, primary_vendor_id
    """, (cutoff_day,)).fetchall()

    for r in rows:
        conn.execute("""
            INSERT OR REPLACE INTO daily_summary (
                day, poi_viewid, shelf_type_id, primary_vendor_id,
                min_price, max_price, avg_price, best_rank, worst_rank, sku_count, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (r["day"], r["poi_viewid"], r["shelf_type_id"], r["primary_vendor_id"],
              r["min_price"], r["max_price"], r["avg_price"],
              r["best_rank"], r["worst_rank"], r["sku_count"],
              datetime.now(timezone.utc).isoformat()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.getenv("CTRIP_DB_PATH", "data/monitor.db"))
    p.add_argument("--raw-dir", default=os.getenv("CTRIP_RAW_DIR", "data/raw_rounds"))
    p.add_argument("--keep-days", type=int, default=30)
    args = p.parse_args()

    import sqlite3
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.keep_days)).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    # 1) 聚合昨天的数据（如果还没聚合）
    aggregate_daily(conn, yesterday)

    # 2) 删除旧数据
    cur = conn.execute("DELETE FROM sku_snapshot WHERE captured_at < ?", (cutoff,))
    print(f"sku_snapshot deleted: {cur.rowcount}")
    cur = conn.execute("DELETE FROM rank_history WHERE captured_at < ?", (cutoff,))
    print(f"rank_history deleted: {cur.rowcount}")
    cur = conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
    print(f"alerts deleted: {cur.rowcount}")
    cur = conn.execute("DELETE FROM rounds WHERE captured_at < ?", (cutoff,))
    print(f"rounds deleted: {cur.rowcount}")

    conn.commit()
    conn.close()

    # 3) raw JSON 文件清理（与 raws 同步）
    raw_dir = Path(args.raw_dir)
    if raw_dir.exists():
        deleted = 0
        for f in raw_dir.glob("*.json"):
            try:
                ts = f.stem[:19].replace("-", ":")  # ISO-ish 前缀
                # 文件名形如 2026-08-24T08-30-00_<viewid>_<rid8>.json
                day_prefix = f.stem[:10].replace("-", "-")
                if day_prefix < cutoff[:10]:
                    f.unlink()
                    deleted += 1
            except Exception:
                pass
        print(f"raw files deleted: {deleted}")


if __name__ == "__main__":
    main()