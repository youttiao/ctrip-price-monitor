"""Session 管理：基于 sqlite 的 sid + bcrypt 验证密码。"""
from __future__ import annotations
import hashlib, secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt

SESSION_TTL_DAYS = 7


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def new_sid() -> str:
    return secrets.token_urlsafe(32)


def login(conn, username: str, password: str, ip: str, ua: str) -> Optional[dict]:
    """验证 → 创建 session → 返回 {'sid', 'user_id', 'username', 'role'}。

    失败返回 None。
    """
    row = conn.execute(
        "SELECT id, pw_hash, role, is_active FROM users WHERE username=?",
        (username,)).fetchone()
    if not row:
        return None
    if not row["is_active"]:
        return None
    if not verify_password(password, row["pw_hash"]):
        return None

    sid = new_sid()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SESSION_TTL_DAYS)
    conn.execute("""
        INSERT INTO sessions (sid, user_id, created_at, expires_at, last_seen_at,
                              ip_prefix, user_agent, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (sid, row["id"], now.isoformat(), expires.isoformat(), now.isoformat(),
          ip[:24], ua[:200]))
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?",
                 (now.isoformat(), row["id"]))
    conn.commit()
    return {"sid": sid, "user_id": row["id"], "username": username,
            "role": row["role"]}


def get_session(conn, sid: str) -> Optional[dict]:
    if not sid:
        return None
    row = conn.execute("""
        SELECT s.sid, s.user_id, s.expires_at, s.is_active, u.username, u.role
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.sid=?
    """, (sid,)).fetchone()
    if not row or not row["is_active"]:
        return None
    if row["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None
    # 滑动过期
    conn.execute("UPDATE sessions SET last_seen_at=? WHERE sid=?",
                 (datetime.now(timezone.utc).isoformat(), sid))
    conn.commit()
    return {"sid": sid, "user_id": row["user_id"], "username": row["username"],
            "role": row["role"]}


def logout(conn, sid: str):
    conn.execute("UPDATE sessions SET is_active=0 WHERE sid=?", (sid,))
    conn.commit()


def current_user(request, conn):
    """FastAPI request → user dict or None。"""
    sid = request.cookies.get("ctrip_sid") or request.headers.get("X-Session")
    return get_session(conn, sid)