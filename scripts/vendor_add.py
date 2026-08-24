#!/usr/bin/env python3
"""直接 SQLite 写入 my_vendors + vendors stub（绕过 admin auth）。

Usage: vendor_add.py <vendor_id> [<label>]
"""
import os, sqlite3, sys
from datetime import datetime, timezone

VID = sys.argv[1]
LABEL = sys.argv[2] if len(sys.argv) > 2 else ""

db_path = os.environ.get("CTRIP_DB_PATH", "data/monitor.db")
conn = sqlite3.connect(db_path, isolation_level=None)
now = datetime.now(timezone.utc).isoformat()

# 1) upsert into my_vendors
cur = conn.execute(
    "SELECT is_active, label FROM my_vendors WHERE vendor_id=?",
    (VID,),
)
row = cur.fetchone()
if row is None:
    conn.execute(
        "INSERT INTO my_vendors (vendor_id, label, is_active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
        (VID, LABEL, now, now),
    )
    print(f"my_vendors: inserted vid={VID} label='{LABEL}'")
else:
    conn.execute(
        "UPDATE my_vendors SET is_active=1, updated_at=?, label=COALESCE(NULLIF(?, ''), label) WHERE vendor_id=?",
        (now, LABEL, VID),
    )
    print(f"my_vendors: reactivated vid={VID}")

# 2) stub into vendors
cur = conn.execute("SELECT 1 FROM vendors WHERE vendor_id=?", (VID,))
if cur.fetchone() is None:
    conn.execute(
        "INSERT OR IGNORE INTO vendors (vendor_id, name, brand_company_name, licence_no, first_seen_at, last_seen_at, sku_count) VALUES (?, NULL, NULL, NULL, ?, NULL, 0)",
        (VID, now),
    )
    print(f"vendors: stub inserted vid={VID}")
else:
    print(f"vendors: existing vid={VID}, untouched")

print(f"OK: vid={VID} is now active in my_vendors + visible in /admin/vendors")