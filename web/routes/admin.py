"""Admin 路由：vendor list + watchlist toggle + config + API secret 管理 + ops + POI 管理。
build-tag: 2026-08-24T14
"""
from __future__ import annotations
import json as _json
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Cookie, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth
from ctrip_core import poi_discovery

ROOT = Path(__file__).resolve().parents[2]

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
    # 双源 UNION：vendors 表 ∪ my_vendors 表（避免 admin 手动加的 vendor 因从未被抓取而看不到）
    rows = conn.execute("""
        SELECT vendor_id, name, brand_company_name, licence_no,
               last_seen_at, sku_count, label, is_active, source
        FROM (
            SELECT v.vendor_id, v.name, v.brand_company_name, v.licence_no,
                   v.last_seen_at, v.sku_count,
                   m.label, m.is_active, 'vendors' AS source
            FROM vendors v
            LEFT JOIN my_vendors m ON m.vendor_id=v.vendor_id
            UNION ALL
            SELECT m.vendor_id, NULL AS name, NULL AS brand_company_name, NULL AS licence_no,
                   NULL AS last_seen_at, 0 AS sku_count,
                   m.label, m.is_active, 'my_only' AS source
            FROM my_vendors m
            LEFT JOIN vendors v ON v.vendor_id=m.vendor_id
            WHERE v.vendor_id IS NULL
        )
        ORDER BY is_active DESC, last_seen_at DESC NULLS LAST, vendor_id
        LIMIT 200
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
    # 1) upsert 到 my_vendors
    conn.execute("""
        INSERT INTO my_vendors (vendor_id, label, is_active, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(vendor_id) DO UPDATE SET
            is_active=1, updated_at=?, label=COALESCE(NULLIF(?, ''), my_vendors.label)
    """, (vendor_id, label, now, now, now, label))
    # 2) stub row 到 vendors 表（确保列表能看到；后续被真实抓到时 upsert 补全信息）
    conn.execute("""
        INSERT OR IGNORE INTO vendors (vendor_id, name, brand_company_name, licence_no,
                                       first_seen_at, last_seen_at, sku_count)
        VALUES (?, NULL, NULL, NULL, ?, NULL, 0)
    """, (vendor_id, now, now))
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


# ── 手动触发抓取与解析 ─────────────────────────────────────────────

# 同进程内同时只允许 1 个解析任务跑；新触发会丢弃（不排队）
_parser_lock = threading.Lock()
_last_trigger: dict = {"ts": None, "ok": None, "detail": None}


@router.post("/capture/trigger", response_class=JSONResponse)
def capture_trigger(request: Request):
    """手动触发：
    1) 给每个 enabled POI 写一条 'capture_now' 命令到 extension_commands
       → 后台扩展每 30s 轮询消费。命令队列是「粘性」的：
       关浏览器期间指令留在 DB，扩展下次启动照样拉。
    2) 同步 spawn round_parser 处理已存在的 pending round（即 fetch 已经落盘
       但还没解析的）。新指令的产物会在扩展回写后由后续 parser 处理。

    返回值：
      queued           下发给扩展的命令数
      parsed_rounds    本次同步解析的 round 数
      parsed_skus      本次产生的 SKU 数（informational）
      extension_alive  最近心跳 < 60min
    """
    user = _require_admin(request)
    conn = request.app.state.db

    # 1) 给每个 enabled POI 写 capture_now 指令
    pois = conn.execute(
        "SELECT viewid, name FROM pois WHERE enabled=1 ORDER BY viewid"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    queued = 0
    for p in pois:
        # 同 POI 短时间内去重，避免按钮被狂点累积成山
        recent = conn.execute("""
            SELECT id FROM extension_commands
            WHERE cmd='capture_now' AND consumed_at IS NULL
              AND json_extract(args_json, '$.viewid') = ?
              AND created_at > datetime(?, '-30 seconds')
            LIMIT 1
        """, (p["viewid"], now)).fetchone()
        if recent:
            continue
        conn.execute("""
            INSERT INTO extension_commands (cmd, args_json, created_at, poll_after_at)
            VALUES ('capture_now', ?, ?, NULL)
        """, (_json.dumps({"viewid": p["viewid"], "name": p["name"]}), now))
        queued += 1
    conn.commit()

    # 2) 同步跑 parser 处理已入库的 pending round
    parser_stdout = ""
    parser_rc = 0
    parser_busy = False
    if _parser_lock.locked():
        parser_busy = True
    else:
        def _run_parser():
            nonlocal parser_stdout, parser_rc
            with _parser_lock:
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "scripts.round_parser", "--limit", "50"],
                        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
                    )
                    parser_stdout = result.stdout[-1500:]
                    parser_rc = result.returncode
                except subprocess.TimeoutExpired:
                    parser_stdout = "parser timeout (>120s)"
                    parser_rc = -1
                except Exception as e:
                    parser_stdout = f"parser error: {e}"
                    parser_rc = -2
        t = threading.Thread(target=_run_parser, daemon=True)
        t.start()
        t.join(timeout=130)

    parsed_skus = 0
    if not parser_busy and parser_rc == 0:
        # 从 stdout 抽 "parsed N SKUs" 行汇总
        for line in parser_stdout.splitlines():
            if "parsed" in line and "SKU" in line:
                m = line.split("parsed")[1].split("SKU")[0].strip()
                try:
                    parsed_skus += int(m)
                except ValueError:
                    pass

    # 3) 扩展心跳判定
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

    global _last_trigger
    _last_trigger = {
        "ts": now,
        "queued": queued,
        "parsed_skus": parsed_skus,
        "extension_alive": ext_alive,
        "parser": {
            "busy": parser_busy, "rc": parser_rc, "stdout_tail": parser_stdout[-600:],
        },
    }
    return JSONResponse({"ok": True, **_last_trigger})


@router.get("/capture/poll", response_class=JSONResponse)
def capture_poll(request: Request):
    """前端轮询：最近 N 分钟内新 round 数 + 扩展心跳 + 已消费/未消费命令数。"""
    _require_admin(request)
    conn = request.app.state.db
    # 最近 5 分钟内 round
    rows = conn.execute("""
        SELECT id, poi_viewid, source, received_at, status, sku_count
        FROM rounds WHERE received_at > datetime('now', '-5 minutes')
        ORDER BY id DESC LIMIT 20
    """).fetchall()
    new_rounds = [{
        "id": r["id"], "viewid": r["poi_viewid"], "source": r["source"],
        "received_at": r["received_at"], "status": r["status"], "sku_count": r["sku_count"] or 0
    } for r in rows]
    # 命令队列状态
    q = conn.execute("""
        SELECT
          SUM(CASE WHEN consumed_at IS NULL THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN consumed_at IS NOT NULL THEN 1 ELSE 0 END) AS consumed
        FROM extension_commands WHERE created_at > datetime('now', '-1 hour')
    """).fetchone()
    hb = conn.execute(
        "SELECT last_polled_at, last_version, last_commands_returned FROM extension_heartbeat WHERE id=1"
    ).fetchone()
    return {
        "ok": True,
        "new_rounds": new_rounds,
        "extension": {
            "last_polled_at": hb["last_polled_at"] if hb else None,
            "last_commands_returned": hb["last_commands_returned"] if hb else 0,
            "last_version": hb["last_version"] if hb else None,
        },
        "queue": {"pending": q["pending"] or 0, "consumed": q["consumed"] or 0},
    }


@router.get("/capture/last", response_class=JSONResponse)
def capture_last(request: Request):
    """查看上一次手动触发的结果。"""
    user = _require_admin(request)
    return JSONResponse(_last_trigger)


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


# ── API secret 管理 ─────────────────────────────────────

def _read_api_secret(conn) -> tuple[str, str | None]:
    """返回 (secret, updated_at)。"""
    row = conn.execute(
        "SELECT value, updated_at FROM config WHERE key='api_secret'"
    ).fetchone()
    if not row:
        return ("", None)
    try:
        return (_json.loads(row["value"]), row["updated_at"])
    except Exception:
        return (row["value"], row["updated_at"])


@router.get("/api-secret", response_class=HTMLResponse)
def admin_api_secret(request: Request):
    user = _require_admin(request)
    conn = request.app.state.db
    secret, updated_at = _read_api_secret(conn)
    # 取最近 N 条 cookie/ingest 用法记录（last_used_at 不在 schema 里，靠 alerts/rounds 间接看）
    last_round = conn.execute(
        "SELECT MAX(received_at) AS last FROM rounds"
    ).fetchone()
    return request.app.state.tmpl.TemplateResponse(
        request, "admin_api_secret.html",
        {"user": user, "api_secret": secret,
         "updated_at": updated_at,
         "last_round_at": last_round["last"] if last_round else None}
    )


@router.post("/api-secret/rotate")
def admin_api_secret_rotate(request: Request):
    """生成新的 api_secret 并立即生效。"""
    user = _require_admin(request)
    conn = request.app.state.db
    new_secret = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO config (key, value, updated_at) VALUES ('api_secret', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
    """, (_json.dumps(new_secret), now))
    conn.commit()
    return RedirectResponse("/admin/api-secret?rotated=1", status_code=303)


# ── POI 管理 ─────────────────────────────────────────────

def _poi_list_rows(conn) -> list:
    """拉所有 POI（含每条最后捕获时间和状态）。"""
    return conn.execute("""
        SELECT p.viewid, p.name, p.enabled, p.district,
               p.last_round_id, p.last_status, p.last_error,
               p.created_at, p.updated_at,
               (SELECT MAX(received_at) FROM rounds r WHERE r.poi_viewid = p.viewid) AS last_captured,
               (SELECT sku_count FROM rounds r
                  WHERE r.poi_viewid = p.viewid
                  ORDER BY received_at DESC LIMIT 1) AS last_sku_count
        FROM pois p
        ORDER BY p.enabled DESC, last_captured DESC NULLS LAST, p.viewid
    """).fetchall()


@router.get("/pois", response_class=HTMLResponse)
def admin_pois(request: Request, error: str | None = None):
    user = _require_admin(request)
    conn = request.app.state.db
    pois = _poi_list_rows(conn)
    add_error = request.query_params.get("error")
    return request.app.state.tmpl.TemplateResponse(
        request, "admin_pois.html",
        {"user": user, "pois": pois, "add_error": add_error}
    )


@router.post("/pois/add")
def admin_pois_add(request: Request,
                   ctrip_url: str = Form(...),
                   name: str = Form("")):
    """从 URL 抽 viewId，写入 pois 表。

    name 可选：留空则记为 "(未命名)"，等下次抓到 round 时被真实名替换。
    """
    user = _require_admin(request)
    conn = request.app.state.db
    viewid = poi_discovery.extract_viewid_from_url(ctrip_url)
    if not viewid:
        return RedirectResponse(
            "/admin/pois?error=" + "URL 里找不到 viewId（试试含 viewId= 或 /sight/N.html 的链接）",
            status_code=303,
        )
    now = datetime.now(timezone.utc).isoformat()
    nm = poi_discovery.canonicalize_poi_name(name) or f"POI-{viewid}"
    conn.execute("""
        INSERT INTO pois (viewid, name, enabled, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        ON CONFLICT(viewid) DO UPDATE SET
            name=COALESCE(NULLIF(?, ''), pois.name),
            enabled=1,
            updated_at=?
    """, (viewid, nm, now, now, nm, now))
    conn.commit()
    return RedirectResponse("/admin/pois", status_code=303)


@router.post("/pois/{viewid}/toggle")
def admin_pois_toggle(request: Request, viewid: int):
    user = _require_admin(request)
    conn = request.app.state.db
    conn.execute("""
        UPDATE pois SET enabled = 1 - enabled, updated_at=?
        WHERE viewid=?
    """, (datetime.now(timezone.utc).isoformat(), viewid))
    conn.commit()
    return RedirectResponse("/admin/pois", status_code=303)


@router.post("/pois/{viewid}/delete")
def admin_pois_delete(request: Request, viewid: int):
    user = _require_admin(request)
    conn = request.app.state.db
    conn.execute("DELETE FROM pois WHERE viewid=?", (viewid,))
    conn.commit()
    return RedirectResponse("/admin/pois", status_code=303)


# 注意：/api/admin/pois/add-via-extension 不在本 router；
# 该 endpoint 在 web/ingest.py（prefix=/api），用 X-API-Secret 认证，
# 供浏览器扩展 popup「同步当前 POI」调用。