"""FastAPI app：dashboard + login + ingest endpoints。"""
from __future__ import annotations
import hmac, json, os, secrets, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Cookie, Depends, Form, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db
from .ingest import router as ingest_router
from .routes.pages import router as pages_router
from .routes.admin import router as admin_router

BJ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _bj_time(v):
    """UTC ISO/datetime → 'YYYY-MM-DD HH:MM' 北京时间。空值原样返回。"""
    if not v:
        return v
    if isinstance(v, str):
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    else:
        dt = v
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ).strftime("%Y-%m-%d %H:%M")


def create_app(db_path: str = None) -> FastAPI:
    db_path = db_path or os.getenv("CTRIP_DB_PATH", "data/monitor.db")
    app = FastAPI(title="携程哨兵")

    # 单 connection（在 1-process 中够用；生产用 uvicorn --workers 2 时考虑连接池）
    app.state.db = db.get_connection(db_path)

    # 幂等建表：扩展命令队列 + 心跳单行表。新部署无需重跑 init_db。
    app.state.db.execute("""
        CREATE TABLE IF NOT EXISTS extension_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cmd TEXT NOT NULL,
            args_json TEXT,
            created_at TEXT NOT NULL,
            poll_after_at TEXT,
            consumed_at TEXT,
            consumed_by TEXT,
            note TEXT
        )
    """)
    app.state.db.execute("""
        CREATE INDEX IF NOT EXISTS idx_extension_commands_unconsumed
            ON extension_commands(poll_after_at) WHERE consumed_at IS NULL
    """)
    app.state.db.execute("""
        CREATE TABLE IF NOT EXISTS extension_heartbeat (
            id INTEGER PRIMARY KEY CHECK(id=1),
            last_polled_at TEXT,
            last_version TEXT,
            last_commands_returned INTEGER DEFAULT 0
        )
    """)
    app.state.db.commit()

    # 静态 + 路由
    static_dir = Path(__file__).parent / "static"
    tmpl_dir = Path(__file__).parent / "templates"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    templates = Jinja2Templates(directory=tmpl_dir)
    templates.env.globals["now"] = lambda: datetime.now(timezone.utc)
    templates.env.filters["bj_time"] = _bj_time

    def _minutes_since(iso_or_dt):
        """UTC ISO / datetime → 距今分钟数（int）。失败返回 None。"""
        if not iso_or_dt:
            return None
        try:
            dt = (datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
                  if isinstance(iso_or_dt, str) else iso_or_dt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
        except Exception:
            return None

    def heartbeat(db_conn):
        """报头心跳：上次捕获 + 距今分钟数 + pulse + cookie 上次同步时间 + 扩展心跳 + 命令队列。"""
        try:
            row = db_conn.execute(
                "SELECT MAX(received_at) AS last FROM rounds WHERE status='parsed'"
            ).fetchone()
            last = row["last"] if row else None
        except Exception:
            last = None
        try:
            cnt = db_conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        except Exception:
            cnt = 0
        try:
            row = db_conn.execute("SELECT MAX(uploaded_at) AS last FROM cookies").fetchone()
            last_cookie = row["last"] if row else None
        except Exception:
            last_cookie = None
        # 扩展心跳
        try:
            row = db_conn.execute(
                "SELECT last_polled_at, last_version, last_commands_returned FROM extension_heartbeat WHERE id=1"
            ).fetchone()
            ext_last = row["last_polled_at"] if row else None
            ext_version = row["last_version"] if row else None
        except Exception:
            ext_last, ext_version = None, None
        # 命令队列（最近 1 小时）
        try:
            qrow = db_conn.execute("""
                SELECT
                  SUM(CASE WHEN consumed_at IS NULL THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN consumed_at IS NOT NULL THEN 1 ELSE 0 END) AS consumed
                FROM extension_commands WHERE created_at > datetime('now', '-1 hour')
            """).fetchone()
            pending = (qrow["pending"] or 0) if qrow else 0
            consumed = (qrow["consumed"] or 0) if qrow else 0
        except Exception:
            pending, consumed = 0, 0

        last_min = _minutes_since(last)
        if last_min is None:
            pulse = "ok"
        elif last_min > 180:
            pulse = "dead"
        elif last_min > 60:
            pulse = "late"
        else:
            pulse = "ok"
        return {"last_at": last, "last_minutes_ago": last_min,
                "pulse": pulse, "round_count": cnt,
                "last_cookie_at": last_cookie,
                "last_cookie_minutes_ago": _minutes_since(last_cookie),
                "extension_last_polled_at": ext_last,
                "extension_last_polled_minutes_ago": _minutes_since(ext_last),
                "extension_version": ext_version,
                "queue_pending": pending, "queue_consumed_1h": consumed}

    templates.env.globals["heartbeat"] = heartbeat
    app.state.tmpl = templates

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return resp

    # CORS：让浏览器扩展的 MAIN world（页面 origin = m.ctrip.com）也能
    # 直接 fetch 到 /api/ingest/round。Server 端仍以 X-API-Secret 鉴权，
    # 所以开放 `*` 只暴露端点存在，不暴露写入权限。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=3600,
    )

    # 路由
    app.include_router(ingest_router)
    app.include_router(pages_router)
    app.include_router(admin_router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "ts": datetime.now(timezone.utc).isoformat(),
                "build_tag": "v2026-08-24T14-poi-discovery"}

    return app


app = create_app()