"""POST /api/ingest/round + /api/cookies/sync。

单一 secret：所有客户端调用这两个 endpoint 必须带 `X-API-Secret` 头，
值等于 config 表里 `api_secret` 键的值（首次 init_db 时随机生成，
admin 可在 `/admin/api-secret` 页面查看 + rotate）。
"""
from __future__ import annotations
import json, os, secrets, uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["ingest"])

RAW_DIR = Path(os.getenv("CTRIP_RAW_DIR", "data/raw_rounds"))
RAW_DIR.mkdir(parents=True, exist_ok=True)


def _api_secret(conn) -> str:
    """从 config 表读 api_secret（DB 是 source of truth）。"""
    row = conn.execute("SELECT value FROM config WHERE key='api_secret'").fetchone()
    if not row:
        return ""
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def _verify(conn, provided: str | None) -> bool:
    import hmac
    exp = _api_secret(conn)
    return bool(exp) and bool(provided) and hmac.compare_digest(
        provided.encode(), exp.encode())


@router.post("/ingest/round")
async def ingest_round(
    request: Request,
    x_api_secret: str | None = Header(default=None, alias="X-API-Secret"),
    x_extension_ver: str | None = Header(default=None, alias="X-Extension-Ver"),
    x_source: str = Header(default="extension", alias="X-Source"),
):
    conn = request.app.state.db
    if not _verify(conn, x_api_secret):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    body = await request.json()
    poi = body.get("poi") or {}
    viewid = poi.get("viewid")
    if not viewid:
        return JSONResponse({"ok": False, "error": "missing poi.viewid"}, status_code=400)

    captured_at = body.get("capturedAt") or datetime.now(timezone.utc).isoformat()
    round_id = str(uuid.uuid4())
    fname = f"{captured_at.replace(':','-')}_{viewid}_{round_id[:8]}.json"
    path = RAW_DIR / fname
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    cur = conn.execute("""
        INSERT INTO rounds (round_id, captured_at, received_at, poi_viewid, poi_name,
                            source, requests_count, status, raw_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (round_id, captured_at, datetime.now(timezone.utc).isoformat(),
          int(viewid), poi.get("name"), x_source,
          len(body.get("requests") or []), str(path)))
    conn.commit()
    return {"ok": True, "round_id": round_id, "round_pk": cur.lastrowid}


@router.post("/cookies/sync")
async def sync_cookies(
    request: Request,
    x_api_secret: str | None = Header(default=None, alias="X-API-Secret"),
):
    conn = request.app.state.db
    if not _verify(conn, x_api_secret):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    body = await request.json()
    cookies = body.get("cookies") or {}
    if not cookies.get("GUID"):
        return JSONResponse({"ok": False, "error": "missing GUID"}, status_code=400)

    blob = json.dumps(cookies, ensure_ascii=False)
    conn.execute("""
        INSERT INTO cookies (blob_json, uploaded_at, source, uploaded_by)
        VALUES (?, ?, 'extension', 'extension')
    """, (blob, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return {"ok": True}