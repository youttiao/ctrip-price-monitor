"""POST /api/ingest/round + /api/cookies/sync。"""
from __future__ import annotations
import hmac, json, os, secrets, uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api", tags=["ingest"])

RAW_DIR = Path(os.getenv("CTRIP_RAW_DIR", "data/raw_rounds"))
RAW_DIR.mkdir(parents=True, exist_ok=True)


def _verify(provided: str | None, env_key: str) -> bool:
    exp = os.getenv(env_key, "")
    return bool(exp) and bool(provided) and hmac.compare_digest(
        provided.encode(), exp.encode())


@router.post("/ingest/round")
async def ingest_round(
    request: Request,
    x_ingest_secret: str | None = Header(default=None, alias="X-Ingest-Secret"),
    x_extension_ver: str | None = Header(default=None, alias="X-Extension-Ver"),
    x_source: str = Header(default="extension", alias="X-Source"),
):
    if not _verify(x_ingest_secret, "INGEST_SECRET"):
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

    conn = request.app.state.db
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
    x_cookie_secret: str | None = Header(default=None, alias="X-Cookie-Secret"),
):
    if not _verify(x_cookie_secret, "COOKIE_SYNC_SECRET"):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    body = await request.json()
    cookies = body.get("cookies") or {}
    if not cookies.get("GUID"):
        return JSONResponse({"ok": False, "error": "missing GUID"}, status_code=400)

    conn = request.app.state.db
    blob = json.dumps(cookies, ensure_ascii=False)
    conn.execute("""
        INSERT INTO cookies (blob_json, uploaded_at, source, uploaded_by)
        VALUES (?, ?, 'extension', 'extension')
    """, (blob, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return {"ok": True}