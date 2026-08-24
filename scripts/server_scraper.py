#!/usr/bin/env python3
"""Server-side scraper：用最新 cookies 调 search + addInfo + priceCalendar。

30 分钟一轮。每轮：
    1. 读 cookies 表最新一条
    2. 对每个 enabled POI：
       - search（distributorSearchObj）→ 拿 resources + poiId
       - 对每个 resource 调 resourceAddInfo 拿 vendor
       - 对每个 resource 调 getProductPriceCalendar 拿每日/票种价格（如果能拿到 poiId）
       - POST 到本地 ingest endpoint（带 X-Ingest-Secret）

注意事项：
    - 只调 search + addInfo + priceCalendar，不调 getProductShelf（后者要 w-payload-source header）
    - 如果没有 cookies，跳过本轮
    - priceCalendar 用 URL 路径里的 poiId（不是 viewid），从 search 响应抽，pois 字典兜底
"""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# 让脚本能 `import ctrip_core` 不论从哪儿执行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctrip_core import selectors as S  # noqa: E402

# 日历调用之间的 sleep，避免瞬时高并发触发风控
_CALENDAR_THROTTLE_SEC = 0.3


def load_latest_cookies(db_path: str) -> dict | None:
    import sqlite3
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT blob_json FROM cookies ORDER BY uploaded_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0])


def load_api_secret(db_path: str) -> str:
    """从 config 表读 api_secret（admin 在 /admin/api-secret rotate 后立刻生效）。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT value FROM config WHERE key='api_secret'"
    ).fetchone()
    conn.close()
    if not row:
        return ""
    try:
        return json.loads(row[0])
    except Exception:
        return row[0]


def load_enabled_pois(db_path: str) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT viewid, name, district FROM pois WHERE enabled=1 ORDER BY viewid"
    ).fetchall()
    conn.close()
    return [{"viewid": r[0], "name": r[1], "district": r[2]} for r in rows]


def fetch_search(client: httpx.Client, cookies: dict, poi: dict, use_date: str | None = None) -> dict:
    """调一次 search（distributorSearchObj）。"""
    body = S.search_payload(poi["viewid"], use_date=use_date)
    r = client.post(S.SEARCH_URL, json=body, headers=S.build_headers(cookies, "search"))
    r.raise_for_status()
    return r.json()


def fetch_add_info(client: httpx.Client, cookies: dict, resource_id: int, poi_viewid: int) -> dict:
    body = S.addinfo_payload(resource_id, poi_viewid)
    r = client.post(S.ADDINFO_URL, json=body, headers=S.build_headers(cookies, "addinfo"))
    r.raise_for_status()
    return r.json()


def fetch_price_calendar(client: httpx.Client, cookies: dict, rid: int,
                         poi_id_str: str, viewid: int) -> dict:
    """调一次 getProductPriceCalendar。服务器端可调,无需 w-payload-source。

    poi_id_str 必须是 URL 路径里的 poiId (字符串),不是 viewid。
    """
    body = S.price_calendar_payload(rid, poi_id_str, cookies)
    r = client.post(S.PRICE_CAL_URL, json=body,
                    headers=S.build_headers(cookies, "calendar"))
    r.raise_for_status()
    return r.json()


def collect_round(poi: dict, cookies: dict) -> dict:
    """组装一个 round：search + 每 rid 的 addInfo + 每 rid 的 priceCalendar。

    Returns: dict that matches the extension ingest schema (poi + requests).
    所有响应都以 `body` 字段返回（与 parse.py:_extract_body 的识别格式对齐）。
    """
    captured = datetime.now(timezone.utc).isoformat()
    requests: list[dict] = []

    with httpx.Client(timeout=20) as client:
        # 1) search
        try:
            search_resp = fetch_search(client, cookies, poi)
            requests.append({
                "url": S.SEARCH_URL,
                "method": "POST",
                "postData": {"text": json.dumps(S.search_payload(poi["viewid"]))},
                "body": search_resp,
            })
        except Exception as e:
            return {"capturedAt": captured, "poi": poi, "error": f"search failed: {e}", "requests": []}

        # 2) addInfo + priceCalendar for each resource
        resources = S.extract_search_resources(search_resp)
        for rid, poi_id_str in resources[:S.MAX_ADDINFO_PER_ROUND]:
            # addInfo
            try:
                info = fetch_add_info(client, cookies, rid, poi["viewid"])
                requests.append({
                    "url": S.ADDINFO_URL,
                    "method": "POST",
                    "postData": {"text": json.dumps(S.addinfo_payload(rid, poi["viewid"]))},
                    "body": info,
                })
            except Exception as e:
                # 不让单条 addInfo 失败搞砸整轮
                requests.append({
                    "url": S.ADDINFO_URL,
                    "method": "POST",
                    "postData": {"text": json.dumps(S.addinfo_payload(rid, poi["viewid"]))},
                    "body": {"_error": str(e)},
                })

            # priceCalendar (新)
            effective_poi_id = poi_id_str or S.POI_VIEWID_TO_POI_ID.get(poi["viewid"])
            if not effective_poi_id:
                print(f"  [calendar] skip rid={rid}: no poiId (search 响应无 poiId,且不在 POI_VIEWID_TO_POI_ID 兜底表里)",
                      file=sys.stderr)
                continue
            try:
                cal = fetch_price_calendar(client, cookies, rid, effective_poi_id, poi["viewid"])
                requests.append({
                    "url": S.PRICE_CAL_URL,
                    "method": "POST",
                    "postData": {"text": json.dumps(S.price_calendar_payload(rid, effective_poi_id, cookies))},
                    "body": cal,
                })
            except Exception as e:
                requests.append({
                    "url": S.PRICE_CAL_URL,
                    "method": "POST",
                    "postData": {"text": json.dumps(S.price_calendar_payload(rid, effective_poi_id, cookies))},
                    "body": {"_error": str(e)},
                })
            time.sleep(_CALENDAR_THROTTLE_SEC)

    return {
        "capturedAt": captured,
        "poi": poi,
        "requests": requests,
    }


def post_round(server: str, secret: str, payload: dict) -> tuple[int, dict]:
    r = httpx.post(
        f"{server.rstrip('/')}/api/ingest/round",
        json=payload,
        headers={"X-API-Secret": secret, "X-Source": "server"},
        timeout=15,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.getenv("CTRIP_DB_PATH", "data/monitor.db"))
    p.add_argument("--server", default=os.getenv("CTRIP_SERVER", "http://127.0.0.1:8000"))
    args = p.parse_args()

    # api_secret 始终从 DB 读（与 /admin/api-secret 同步）
    secret = load_api_secret(args.db)
    if not secret:
        print("FATAL: api_secret not set in config table; run init_db", file=sys.stderr)
        sys.exit(2)

    cookies = load_latest_cookies(args.db)
    if not cookies:
        print("SKIP: no cookies in DB yet")
        return

    pois = load_enabled_pois(args.db)
    if not pois:
        print("SKIP: no enabled POIs")
        return

    for poi in pois:
        print(f"[{poi['viewid']}] {poi['name']} ...", flush=True)
        try:
            payload = collect_round(poi, cookies)
        except Exception as e:
            print(f"  collect failed: {e}", file=sys.stderr)
            continue
        if not payload.get("requests"):
            print(f"  no requests, skip")
            continue
        code, resp = post_round(args.server, args.secret, payload)
        print(f"  -> {code} {resp.get('round_id','?')[:8] if isinstance(resp, dict) else resp}")


if __name__ == "__main__":
    main()