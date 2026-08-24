"""FastAPI app：dashboard + login + ingest endpoints。"""
from __future__ import annotations
import hmac, json, os, secrets, uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Cookie, Depends, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db
from .ingest import router as ingest_router
from .routes.pages import router as pages_router
from .routes.admin import router as admin_router


def create_app(db_path: str = None) -> FastAPI:
    db_path = db_path or os.getenv("CTRIP_DB_PATH", "data/monitor.db")
    app = FastAPI(title="携程哨兵")

    # 单 connection（在 1-process 中够用；生产用 uvicorn --workers 2 时考虑连接池）
    app.state.db = db.get_connection(db_path)

    # 静态 + 路由
    static_dir = Path(__file__).parent / "static"
    tmpl_dir = Path(__file__).parent / "templates"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    templates = Jinja2Templates(directory=tmpl_dir)
    templates.env.globals["now"] = lambda: datetime.now(timezone.utc)

    def heartbeat(db_conn):
        """报头心跳：上次捕获 + 距今分钟数 + pulse 状态。"""
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
        last_min = None
        pulse = "ok"
        if last:
            try:
                if isinstance(last, str):
                    last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                else:
                    last_dt = last
                last_min = int((datetime.now(timezone.utc) - last_dt).total_seconds() / 60)
                if last_min > 180:
                    pulse = "dead"
                elif last_min > 60:
                    pulse = "late"
            except Exception:
                last_min = None
        return {"last_at": last, "last_minutes_ago": last_min,
                "pulse": pulse, "round_count": cnt}

    templates.env.globals["heartbeat"] = heartbeat
    app.state.tmpl = templates

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return resp

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