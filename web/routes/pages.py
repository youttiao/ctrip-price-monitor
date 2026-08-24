"""Dashboard 页面：login / POI list / POI detail / 我的足迹 / alerts。"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth

router = APIRouter(tags=["pages"])


def _current(request: Request):
    sid = request.cookies.get("ctrip_sid") or ""
    return auth.get_session(request.app.state.db, sid) if sid else None


def _render(request: Request, tpl: str, **ctx):
    templates = request.app.state.tmpl
    user = _current(request)
    ctx.setdefault("user", user)
    return templates.TemplateResponse(request, tpl, ctx)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _render(request, "login.html", error="")


@router.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    conn = request.app.state.db
    ua = request.headers.get("user-agent", "")
    ip = (request.client.host if request.client else "0.0.0.0")
    sess = auth.login(conn, username, password, ip, ua)
    if not sess:
        return _render(request, "login.html", error="用户名或密码错误")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("ctrip_sid", sess["sid"], httponly=True, secure=False,
                    samesite="strict", max_age=7 * 86400)
    return resp


@router.post("/logout")
def logout(request: Request):
    sid = request.cookies.get("ctrip_sid", "")
    if sid:
        auth.logout(request.app.state.db, sid)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("ctrip_sid")
    return resp


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = _current(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = request.app.state.db

    # POI 列表（每个 POI 的最近状态）
    pois = conn.execute("""
        SELECT p.viewid, p.name,
               (SELECT MAX(received_at) FROM rounds WHERE poi_viewid=p.viewid) AS last_received,
               (SELECT COUNT(*) FROM sku_snapshot s
                  JOIN rounds r ON r.id=s.round_id
                  WHERE s.poi_viewid=p.viewid
                    AND r.id=(SELECT MAX(id) FROM rounds WHERE poi_viewid=p.viewid)) AS last_sku_count,
               (SELECT COUNT(*) FROM sku_snapshot s
                  JOIN rounds r ON r.id=s.round_id
                  JOIN my_vendors m ON m.vendor_id=s.primary_vendor_id AND m.is_active=1
                  WHERE s.poi_viewid=p.viewid
                    AND r.id=(SELECT MAX(id) FROM rounds WHERE poi_viewid=p.viewid)) AS last_mine_count
        FROM pois p
        WHERE p.enabled=1
        ORDER BY p.viewid
    """).fetchall()

    # 我的 vendorIds
    my_vids = [r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1 ORDER BY id").fetchall()]

    # 最近 alerts
    alerts = conn.execute("""
        SELECT ts, severity, type, poi_name, shelf_type_name, sku_name, payload
        FROM alerts ORDER BY ts DESC LIMIT 10
    """).fetchall()

    # Watchlist 数量
    wl_count = conn.execute(
        "SELECT COUNT(*) FROM watchlist WHERE user_id=?", (user["user_id"],)
    ).fetchone()[0]

    return _render(request, "index.html", pois=pois, my_vids=my_vids,
                   alerts=alerts, wl_count=wl_count)


@router.get("/poi/{viewid}", response_class=HTMLResponse)
def poi_detail(request: Request, viewid: int):
    user = _current(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = request.app.state.db
    poi = conn.execute("SELECT * FROM pois WHERE viewid=?", (viewid,)).fetchone()
    if not poi:
        raise HTTPException(404, "POI not found")

    # 最近一轮 round 的 SKU（含 shelfType）
    rows = conn.execute("""
        SELECT s.id AS sku_id, s.resource_id, s.primary_vendor_id,
               s.full_name, s.shelf_type_id, s.shelf_type_name,
               s.display_price, s.market_price, s.sale_count,
               v.brand_company_name,
               m.vendor_id AS is_mine_vid,
               rh.rank, rh.gap, rh.lowest_price
        FROM rounds r
        JOIN sku_snapshot s ON s.round_id=r.id
        LEFT JOIN vendors v ON v.vendor_id=s.primary_vendor_id
        LEFT JOIN my_vendors m ON m.vendor_id=s.primary_vendor_id AND m.is_active=1
        LEFT JOIN rank_history rh ON rh.round_id=r.id AND rh.resource_id=s.resource_id
        WHERE r.poi_viewid=?
          AND r.id=(SELECT MAX(id) FROM rounds WHERE poi_viewid=? AND status='parsed')
        ORDER BY s.shelf_type_id NULLS LAST, rh.rank NULLS LAST, s.display_price
    """, (viewid, viewid)).fetchall()

    # 我的 watchlist 中这个 POI 的 shelfType
    watched = {r["shelf_type_id"] for r in conn.execute("""
        SELECT DISTINCT shelf_type_id FROM watchlist
        WHERE user_id=? AND poi_viewid=?
    """, (user["user_id"], viewid)).fetchall()}

    # 我的 vendorIds
    my_vids = {r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1").fetchall()}

    return _render(request, "poi.html", poi=poi, rows=rows,
                   watched=watched, my_vids=my_vids)


@router.get("/myfootprint", response_class=HTMLResponse)
def myfootprint(request: Request):
    user = _current(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    conn = request.app.state.db

    # 所有 POI × 我出现的 shelfType
    rows = conn.execute("""
        WITH last_round AS (
            SELECT poi_viewid, MAX(id) AS rid FROM rounds WHERE status='parsed' GROUP BY poi_viewid
        )
        SELECT s.poi_viewid, p.name AS poi_name,
               s.shelf_type_id, s.shelf_type_name,
               COUNT(DISTINCT s.resource_id) AS sku_count,
               MAX(s.display_price) AS max_price,
               MIN(s.display_price) AS min_price,
               (SELECT COUNT(*) FROM watchlist w
                  WHERE w.user_id=? AND w.poi_viewid=s.poi_viewid
                    AND w.shelf_type_id=s.shelf_type_id) AS is_watched
        FROM sku_snapshot s
        JOIN last_round lr ON lr.rid=s.round_id
        JOIN pois p ON p.viewid=s.poi_viewid
        JOIN my_vendors m ON m.vendor_id=s.primary_vendor_id AND m.is_active=1
        WHERE s.shelf_type_id IS NOT NULL
        GROUP BY s.poi_viewid, s.shelf_type_id
        ORDER BY s.poi_viewid, s.shelf_type_id
    """, (user["user_id"],)).fetchall()

    # 重组为 poi 分组
    by_poi = {}
    for r in rows:
        d = dict(r)
        by_poi.setdefault(d["poi_viewid"], {"name": d["poi_name"], "shelves": []})
        by_poi[d["poi_viewid"]]["shelves"].append(d)

    return _render(request, "myfootprint.html",
                   by_poi=by_poi, my_vids=[r["vendor_id"] for r in
                                            conn.execute("SELECT vendor_id FROM my_vendors WHERE is_active=1")
                                            .fetchall()])


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    user = _current(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    conn = request.app.state.db
    alerts = conn.execute("""
        SELECT ts, severity, type, poi_viewid, poi_name,
               shelf_type_id, shelf_type_name, sku_name, vendor_id,
               payload, webhook_status
        FROM alerts ORDER BY ts DESC LIMIT 200
    """).fetchall()
    return _render(request, "alerts.html", alerts=alerts)


@router.get("/api/shelf/{shelf_id}/rank_history")
def api_shelf_rank_history(request: Request, shelf_id: int):
    """返回某 shelfType 的最近 7 天 rank 序列（按 round 时间）。"""
    user = _current(request)
    if not user:
        raise HTTPException(401)
    conn = request.app.state.db
    rows = conn.execute("""
        SELECT r.captured_at, rh.vendor_id, rh.rank, rh.display_price,
               rh.lowest_price, rh.is_mine
        FROM rank_history rh
        JOIN rounds r ON r.id=rh.round_id
        WHERE rh.shelf_type_id=? AND rh.is_mine=1
        ORDER BY r.captured_at DESC
        LIMIT 50
    """, (shelf_id,)).fetchall()
    return {"shelf_id": shelf_id, "history": [dict(r) for r in reversed(list(rows))]}