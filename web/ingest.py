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


@router.post("/admin/pois/add-via-extension")
async def admin_pois_add_via_extension(
    request: Request,
    x_api_secret: str | None = Header(default=None, alias="X-API-Secret"),
):
    """浏览器扩展 popup「同步当前 POI」调用。

    body: { viewid: int, name?: str, pageUrl?: str }
    不存在则 INSERT，存在则更新 name 并 enable。
    """
    # 延迟导入（路由归属问题：poi_discovery 在 ctrip_core/）
    from ctrip_core.poi_discovery import extract_viewid_from_url, canonicalize_poi_name  # noqa

    conn = request.app.state.db
    if not _verify(conn, x_api_secret):
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    viewid = body.get("viewid")
    if not isinstance(viewid, int):
        try:
            viewid = int(str(viewid).strip())
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "missing viewid"}, status_code=400)

    name = canonicalize_poi_name(body.get("name") or "")
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("SELECT 1 FROM pois WHERE viewid=?", (viewid,)).fetchone()
    if cur is None:
        nm = name or f"POI-{viewid}"
        conn.execute("""
            INSERT INTO pois (viewid, name, enabled, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
        """, (viewid, nm, now, now))
        action = "inserted"
    else:
        if name:
            conn.execute("""
                UPDATE pois SET name=?, enabled=1, updated_at=?
                WHERE viewid=?
            """, (name, now, viewid))
            action = "name_updated"
        else:
            conn.execute("""
                UPDATE pois SET enabled=1, updated_at=? WHERE viewid=?
            """, (now, viewid))
            action = "enabled"
    conn.commit()
    return JSONResponse({"ok": True, "viewid": viewid, "action": action,
                         "name": name or None})