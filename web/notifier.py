"""Webhook 推送：通用 JSON payload + HMAC 签名 + 重试。"""
from __future__ import annotations
import hashlib, hmac, json, urllib.request, urllib.error, urllib.parse
from typing import Optional


def format_payload(alert: dict) -> dict:
    """从 alerts 表的一行格式化 webhook payload。"""
    import json as _json
    payload_inner = _json.loads(alert.get("payload", "{}"))
    poi_viewid = alert.get("poi_viewid")
    dashboard = f"https://xiecheng.19880913.xyz/poi/{poi_viewid}" if poi_viewid else None
    title = _build_title(alert)
    return {
        "title": title,
        "severity": alert.get("severity"),
        "ts": alert.get("ts"),
        "dedup_key": alert.get("dedup_key"),
        "poi": {"viewid": poi_viewid, "name": alert.get("poi_name")},
        "shelf_type": {"id": alert.get("shelf_type_id"),
                       "name": alert.get("shelf_type_name")},
        "sku": {"name": alert.get("sku_name"),
                "vendor_id": alert.get("vendor_id")},
        "type_detail": payload_inner,
        "links": {"dashboard": dashboard},
    }


def _build_title(alert: dict) -> str:
    poi = alert.get("poi_name") or f"viewid={alert.get('poi_viewid')}"
    shelf = alert.get("shelf_type_name") or f"shelf={alert.get('shelf_type_id')}"
    sku = alert.get("sku_name") or f"vendor={alert.get('vendor_id')}"
    typ = alert.get("type")
    if typ == "rank_drop":
        return f"⚠️ {poi} · {shelf}：{sku} 从 #{payload_old(alert)} 掉到 #{payload_new(alert)}"
    if typ == "rank_up":
        return f"✅ {poi} · {shelf}：{sku} 升到 #{payload_new(alert)}"
    if typ == "disappeared":
        return f"🚨 {poi} · {shelf}：{sku} 已不在该货架！"
    if typ == "appeared":
        return f"ℹ️ {poi} · {shelf}：{sku} 新进入（当前 #{payload_new(alert)}）"
    if typ == "still_non_first":
        return f"⚠️ {poi} · {shelf}：{sku} 仍非 #1（当前 #{payload_new(alert)}）"
    return f"{poi} · {shelf}：{typ}"


def payload_old(alert):
    import json as _json
    return _json.loads(alert.get("payload", "{}")).get("old_rank", "?")


def payload_new(alert):
    import json as _json
    return _json.loads(alert.get("payload", "{}")).get("new_rank",
                  _json.loads(alert.get("payload", "{}")).get("rank", "?"))


def send_webhook(url: str, secret: Optional[str], payload: dict,
                 dedup_key: str = "", max_retries: int = 2) -> tuple[bool, str]:
    """POST JSON 到 url，HMAC 签名。返回 (ok, response_body)。

    不抛异常；总是返回结果，便于批量发送。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Signature"] = sig
    headers["X-Dedup-Key"] = dedup_key

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return True, resp.read().decode("utf-8", errors="replace")[:500]
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
            if e.code < 500:
                break  # 4xx 不重试
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err