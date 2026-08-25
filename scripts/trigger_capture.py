#!/usr/bin/env python3
"""Auto-trigger capture_now for all enabled POIs (systemd timer 15min cycle).

直接走 DB（Option A：in-process，绕开 admin session 鉴权）。
复用 web/routes/admin.py:capture_trigger 的去重/插入语义，避免按钮被狂点累积成山。

dedup 规则：同 viewid 的 unconsumed capture_now 在过去 30 秒内 → 跳过。
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.db import get_connection  # noqa: E402


def main() -> int:
    db_path = os.getenv("CTRIP_DB_PATH", "data/monitor.db")
    conn = get_connection(db_path)

    pois = conn.execute(
        "SELECT viewid, name FROM pois WHERE enabled=1 ORDER BY viewid"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    queued = 0
    skipped = 0
    for p in pois:
        # dedup：30 秒内同 viewid 的未消费 capture_now → 跳过
        recent = conn.execute("""
            SELECT id FROM extension_commands
            WHERE cmd='capture_now' AND consumed_at IS NULL
              AND json_extract(args_json, '$.viewid') = ?
              AND created_at > datetime(?, '-30 seconds')
            LIMIT 1
        """, (p["viewid"], now)).fetchone()
        if recent:
            skipped += 1
            continue
        conn.execute("""
            INSERT INTO extension_commands (cmd, args_json, created_at, poll_after_at)
            VALUES ('capture_now', ?, ?, NULL)
        """, (json.dumps({"viewid": p["viewid"], "name": p["name"]}), now))
        queued += 1
    conn.commit()

    # 扩展心跳：判定 alive 给运维一个直观指标
    hb = conn.execute(
        "SELECT last_polled_at FROM extension_heartbeat WHERE id=1"
    ).fetchone()
    ext_alive = False
    if hb and hb["last_polled_at"]:
        try:
            last = datetime.fromisoformat(hb["last_polled_at"].replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            ext_alive = (datetime.now(timezone.utc) - last).total_seconds() < 3600
        except Exception:
            pass

    print(f"[trigger_capture] ts={now} enabled_pois={len(pois)} "
          f"queued={queued} skipped={skipped} extension_alive={ext_alive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
