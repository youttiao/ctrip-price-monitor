"""Admin 路由：vendor list + watchlist toggle + config。"""
from __future__ import annotations
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(request: Request):
    sid = request.cookies.get("ctrip_sid", "")
    user = auth.get_session(request.app.state.db, sid) if sid else None
    if not user:
        raise HTTPException(401, "login required")
    if user["role"] != "admin":
        raise HTTPException(403, "admin required")
    return user


@router.get("/vendors", response_class=HTMLResponse)
def vendors(request: Request):
    user = _require_admin(request)
    conn = request.app.state.db
    rows = conn.execute("""
        SELECT v.vendor_id, v.name, v.brand_company_name, v.licence_no,
               v.last_seen_at, v.sku_count,
               m.label, m.is_active
        FROM vendors v
        LEFT JOIN my_vendors m ON m.vendor_id=v.vendor_id
        ORDER BY v.last_seen_at DESC NULLS LAST
        LIMIT 100
    """).fetchall()
    return request.app.state.tmpl.TemplateResponse(
        request, "admin_vendors.html",
        {"user": user, "vendors": rows}
    )


@router.post("/vendors/add")
def vendor_add(request: Request, vendor_id: int = Form(...), label: str = Form("")):
    user = _require_admin(request)
    conn = request.app.state.db
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO my_vendors (vendor_id, label, is_active, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(vendor_id) DO UPDATE SET
            is_active=1, updated_at=?, label=COALESCE(NULLIF(?, ''), my_vendors.label)
    """, (vendor_id, label, now, now, now, label))
    conn.commit()
    return RedirectResponse("/admin/vendors", status_code=303)


@router.post("/vendors/{vendor_id}/toggle")
def vendor_toggle(request: Request, vendor_id: int):
    user = _require_admin(request)
    conn = request.app.state.db
    conn.execute("""
        UPDATE my_vendors SET is_active = 1 - is_active,
                             updated_at = ?
        WHERE vendor_id=?
    """, (datetime.now(timezone.utc).isoformat(), vendor_id))
    conn.commit()
    return RedirectResponse("/admin/vendors", status_code=303)


@router.post("/vendors/{vendor_id}/delete")
def vendor_delete(request: Request, vendor_id: int):
    user = _require_admin(request)
    conn = request.app.state.db
    conn.execute("DELETE FROM my_vendors WHERE vendor_id=?", (vendor_id,))
    conn.commit()
    return RedirectResponse("/admin/vendors", status_code=303)


@router.post("/watchlist/toggle")
def watchlist_toggle(
    request: Request,
    poi_viewid: int = Form(...),
    shelf_type_id: int = Form(...),
    active: str = Form("0"),
):
    user = _require_admin(request)
    conn = request.app.state.db
    now = datetime.now(timezone.utc).isoformat()
    if active == "1":
        conn.execute("""
            INSERT OR IGNORE INTO watchlist (user_id, poi_viewid, shelf_type_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (user["user_id"], poi_viewid, shelf_type_id, now))
    else:
        conn.execute("""
            DELETE FROM watchlist WHERE user_id=? AND poi_viewid=? AND shelf_type_id=?
        """, (user["user_id"], poi_viewid, shelf_type_id))
    conn.commit()
    return {"ok": True}


@router.get("/config", response_class=HTMLResponse)
def admin_config(request: Request):
    user = _require_admin(request)
    conn = request.app.state.db
    cfg = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM config").fetchall()}
    return request.app.state.tmpl.TemplateResponse(
        request, "admin_config.html",
        {"user": user, "config": cfg}
    )


@router.post("/config/webhook")
def admin_config_webhook(
    request: Request,
    webhook_url: str = Form(...),
    webhook_secret: str = Form(""),
):
    user = _require_admin(request)
    conn = request.app.state.db
    import json as _json
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO config (key, value, updated_at) VALUES ('webhook_url', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (_json.dumps(webhook_url or None), now))
    if webhook_secret:
        conn.execute("""
            INSERT INTO config (key, value, updated_at) VALUES ('webhook_secret', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (_json.dumps(webhook_secret), now))
    conn.commit()
    return RedirectResponse("/admin/config", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def admin_users(request: Request):
    user = _require_admin(request)
    conn = request.app.state.db
    users = conn.execute(
        "SELECT id, username, role, is_active, created_at, last_login_at FROM users"
    ).fetchall()
    return request.app.state.tmpl.TemplateResponse(
        request, "admin_users.html",
        {"user": user, "users": users}
    )