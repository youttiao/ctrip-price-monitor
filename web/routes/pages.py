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

    # 我的 vendorIds
    my_vids = {r["vendor_id"] for r in conn.execute(
        "SELECT vendor_id FROM my_vendors WHERE is_active=1").fetchall()}

    # 我的 watchlist 中这个 POI 的 shelfType
    watched = {r["shelf_type_id"] for r in conn.execute("""
        SELECT DISTINCT shelf_type_id FROM watchlist
        WHERE user_id=? AND poi_viewid=?
    """, (user["user_id"], viewid)).fetchall()}

    # 最近一轮 parsed round 的元数据
    last_round = conn.execute("""
        SELECT MAX(id) AS rid, MAX(captured_at) AS captured_at
        FROM rounds WHERE poi_viewid=? AND status='parsed'
    """, (viewid,)).fetchone()
    last_round_id = last_round["rid"] if last_round else None
    last_round_at = last_round["captured_at"] if last_round else None

    # SKU 列表（meta: vendor, shelfType, sales）— 兜底渲染用
    rows = []
    if last_round_id:
        rows = conn.execute("""
            SELECT s.id AS sku_id, s.resource_id, s.primary_vendor_id,
                   s.full_name, s.shelf_type_id, s.shelf_type_name,
                   s.display_price, s.market_price, s.sale_count,
                   v.brand_company_name,
                   (CASE WHEN m.vendor_id IS NULL THEN 0 ELSE 1 END) AS is_mine_vid,
                   rh.rank, rh.gap, rh.lowest_price
            FROM rounds r
            JOIN sku_snapshot s ON s.round_id=r.id
            LEFT JOIN vendors v ON v.vendor_id=s.primary_vendor_id
            LEFT JOIN my_vendors m ON m.vendor_id=s.primary_vendor_id AND m.is_active=1
            LEFT JOIN rank_history rh ON rh.round_id=r.id AND rh.resource_id=s.resource_id
            WHERE r.id=?
            ORDER BY s.shelf_type_id NULLS LAST, rh.rank NULLS LAST, s.display_price
        """, (last_round_id,)).fetchall()

    # shelf 分组（用于顶部关注 chip 与边注）
    shelf_groups_map: dict[int, dict] = {}
    for r in rows:
        st_id = r["shelf_type_id"] or 0
        sg = shelf_groups_map.setdefault(st_id, {
            "id": st_id if st_id else None,
            "name": r["shelf_type_name"] or "（未分组）",
            "ticket_count": 0,
        })
        sg["ticket_count"] += 1
    shelf_groups = list(shelf_groups_map.values())

    # price_day 查询（最近 7 天）
    matrix = _build_calendar_matrix(conn, last_round_id, viewid, my_vids, days=7)
    daily_summary = matrix.pop("daily_summary", {"mine": 0, "them": 0, "only": 0})
    has_vendor_data = matrix.pop("has_vendor_data", False)
    matrix["shelf_groups"] = shelf_groups
    matrix["last_round_at"] = last_round_at

    # 最近 5 条 alerts（按此 POI 过滤）
    alerts = conn.execute("""
        SELECT ts, severity, type, sku_name, payload
        FROM alerts
        WHERE poi_viewid=?
        ORDER BY ts DESC LIMIT 5
    """, (viewid,)).fetchall()
    # 把 payload JSON 拆出简短预览
    import json as _json
    enriched_alerts = []
    for a in alerts:
        d = dict(a)
        try:
            p = _json.loads(d.get("payload") or "{}")
            # 取最短字段作为预览
            preview = p.get("message") or p.get("price_change") or p.get("note") or p.get("type") or ""
            if not preview:
                keys = [k for k in p.keys() if k not in ("poi", "shelf", "sku")]
                if keys:
                    preview = f"{keys[0]}={p[keys[0]]}"
        except Exception:
            preview = ""
        d["payload_preview"] = str(preview)[:60]
        enriched_alerts.append(d)

    return _render(request, "poi.html", poi=poi, rows=rows,
                   watched=watched, my_vids=my_vids,
                   daily_summary=daily_summary, matrix=matrix,
                   has_vendor_data=has_vendor_data,
                   alerts=enriched_alerts)


def _build_calendar_matrix(conn, round_id, viewid, my_vids, days=7):
    """从 price_day 组装 7 天 × N 票种 矩阵。

    Returns dict 包含：
      - dates: [{iso, day, dow, is_today, is_weekend}, ...]
      - tickets: [{resource_id, name, primary_vendor_id, is_mine, sale_count,
                   cells: [{sale_price, winning_vendor_id, vendor_label,
                            available, state, heat}, ...],
                   avg_7, mine_state, mine_ratio, mine_label}, ...]
      - daily_summary: {mine, them, only}
      - has_vendor_data: bool
      - date_range: "08-25 → 08-31"
      - tickets: []
    """
    from datetime import date, timedelta

    if not round_id:
        return {"dates": [], "tickets": [], "daily_summary":
                {"mine": 0, "them": 0, "only": 0},
                "has_vendor_data": False, "date_range": ""}

    # 7 天窗口（北京时间 → UTC 转换在 SQL 不需要，DATE 用 UTC ISO 也对齐到日历）
    today = date.today()
    date_list = [today + timedelta(days=i) for i in range(days)]
    iso_dates = [d.isoformat() for d in date_list]

    # 一次拉全该 round 的 price_day
    placeholders = ",".join("?" * len(iso_dates))
    rows = conn.execute(f"""
        SELECT pd.resource_id, pd.sale_date, pd.sale_price, pd.min_price,
               pd.inventory, pd.available, pd.package_id,
               pd.winning_vendor_id,
               s.full_name AS sku_name, s.primary_vendor_id,
               s.shelf_type_id, s.shelf_type_name,
               s.sale_count,
               v.name AS winning_vendor_name
        FROM price_day pd
        JOIN sku_snapshot s ON s.round_id=pd.round_id AND s.resource_id=pd.resource_id
        LEFT JOIN vendors v ON v.vendor_id=pd.winning_vendor_id
        WHERE pd.round_id=? AND pd.sale_date IN ({placeholders})
        ORDER BY pd.sale_date
    """, (round_id, *iso_dates)).fetchall()

    # 按 resource_id → 按 sale_date → cell
    from collections import defaultdict
    cells_by_rid: dict[int, dict[str, dict]] = defaultdict(dict)
    sku_meta: dict[int, dict] = {}
    for r in rows:
        rid = r["resource_id"]
        if rid not in sku_meta:
            sku_meta[rid] = {
                "name": r["sku_name"] or f"rid {rid}",
                "primary_vendor_id": r["primary_vendor_id"],
                "is_mine": r["primary_vendor_id"] in my_vids,
                "sale_count": r["sale_count"],
            }
        cells_by_rid[rid][r["sale_date"]] = {
            "sale_price": r["sale_price"],
            "min_price": r["min_price"],
            "winning_vendor_id": r["winning_vendor_id"],
            "winning_vendor_name": r["winning_vendor_name"],
            "available": r["available"],
            "package_id": r["package_id"],
        }

    has_vendor_data = any(
        c.get("winning_vendor_id") for cs in cells_by_rid.values() for c in cs.values()
    )

    # 日期头
    today_iso = today.isoformat()
    dates = []
    for d in date_list:
        dow = d.weekday()  # 0=Mon
        dates.append({
            "iso": d.isoformat(),
            "day": f"{d.day:02d}",
            "dow": dow,
            "is_today": d.isoformat() == today_iso,
            "is_weekend": dow >= 5,
        })

    # 票种行：每个 rid 一行，7 个 cell
    tickets = []
    mine_days = them_days = only_days = 0
    for rid, cell_map in cells_by_rid.items():
        meta = sku_meta.get(rid, {})
        cells = []
        prices = []
        mine_count = 0
        them_count = 0
        only_count = 0
        for d in date_list:
            cell = cell_map.get(d.isoformat())
            if not cell or not cell.get("sale_price"):
                cells.append({
                    "sale_price": None,
                    "state": "only",
                    "heat": "heat-low",
                    "vendor_label": "缺",
                    "winning_vendor_id": None,
                })
                only_count += 1
                continue
            sp = cell["sale_price"]
            prices.append(sp)
            vid = cell.get("winning_vendor_id")
            available = cell.get("available")
            # 三态判定
            if vid is None:
                # 无 vendor 信息：归入"独占"（没法判定）
                state = "only"
                only_count += 1
                vendor_label = (cell.get("winning_vendor_name") or "")[:4] or "—"
            elif vid in my_vids:
                state = "mine"
                mine_count += 1
                vendor_label = "我的"
            else:
                state = "them"
                them_count += 1
                vendor_label = (cell.get("winning_vendor_name") or f"v{vid}")[:4]
            # 热力（基于该票种 7 天价格相对位置）
            if not available:
                state = "sold-out"
                heat = "heat-mid"
            else:
                heat = _heat_for(sp, prices)
            cells.append({
                "sale_price": sp,
                "winning_vendor_id": vid,
                "vendor_label": vendor_label,
                "available": available,
                "state": state,
                "heat": heat,
            })

        # 行级汇总
        avg_7 = sum(prices) / len(prices) if prices else 0
        covered = mine_count + them_count + only_count
        if covered == 0:
            mine_state = "none"
            mine_label = "—"
            mine_ratio = 0
        elif mine_count == 0 and only_count == covered:
            mine_state = "only"
            mine_label = "独占"
            mine_ratio = 0
        else:
            mine_state = "mixed"
            mine_ratio = mine_count / max(mine_count + them_count, 1)
            mine_label = f"{mine_count}/{covered}"
        tickets.append({
            "resource_id": rid,
            "name": meta.get("name"),
            "primary_vendor_id": meta.get("primary_vendor_id"),
            "is_mine": meta.get("is_mine"),
            "sale_count": meta.get("sale_count"),
            "cells": cells,
            "avg_7": avg_7,
            "mine_state": mine_state,
            "mine_ratio": mine_ratio,
            "mine_label": mine_label,
        })
        mine_days += mine_count
        them_days += them_count
        only_days += only_count

    # 排序：shelf 分组（已经按 rid 顺序进来），is_mine 优先
    tickets.sort(key=lambda t: (not t["is_mine"], t["name"] or ""))

    # 日期范围
    if dates:
        date_range = f"{dates[0]['iso'][5:]} → {dates[-1]['iso'][5:]}"
    else:
        date_range = ""

    return {
        "dates": dates,
        "tickets": tickets,
        "daily_summary": {"mine": mine_days, "them": them_days, "only": only_days},
        "has_vendor_data": has_vendor_data,
        "date_range": date_range,
    }


def _heat_for(price: float, prices: list[float]) -> str:
    """热力档（基于该票种价格列表的相对位置）。"""
    if not prices or len(prices) < 2:
        return "heat-mid"
    mn, mx = min(prices), max(prices)
    if mx == mn:
        return "heat-low"
    ratio = (price - mn) / (mx - mn)
    if ratio < 0.34:
        return "heat-low"
    if ratio < 0.67:
        return "heat-mid"
    return "heat-high"


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