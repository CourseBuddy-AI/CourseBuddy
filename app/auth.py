"""認證與授權（負責：陳儀珊）。所有檢查都在伺服器端：前端藏按鈕不算存取控制。

共用介面（名稱不要改，其他組員的程式會用到）：
- g.user                     目前登入的使用者（User，有 .id、.role），沒登入是 None
- login_required             沒登入：/api/* 回 401，頁面導向登入頁
- role_required(*roles)      RBAC：角色不在名單內回 403
- can_view_material(u, m)    ABAC：講義只有擁有者能讀寫

另外提供：CSRF 保護（所有 POST 都要帶 token）、登入／登出時的 session 處理。
"""
from __future__ import annotations

import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, current_app, g, redirect, request, session, url_for

from .store import Material, User

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------- Session ----------------
def load_user() -> None:
    """每個請求開始時執行：從簽章 cookie 取出 uid，再到 store 查使用者與目前的角色。"""
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    user = current_app.extensions["store"].users.get(uid)
    # 使用者被刪除，或已經登出過（epoch 變了）→ 這個 cookie 作廢
    if user is None or session.get("epoch") != user.session_epoch:
        session.clear()
        return
    g.user = user


def start_session(user: User) -> None:
    """登入成功時呼叫（示範登入與 Google 登入共用）。"""
    session.clear()              # 防 session fixation：登入時一律換新 session
    session.permanent = True     # 套用 PERMANENT_SESSION_LIFETIME，過期由伺服器驗證簽章時間戳
    session["uid"] = user.id
    session["epoch"] = user.session_epoch
    csrf_token()                 # 新 session 產生新的 CSRF token
    current_app.logger.info("login uid=%s role=%s", user.id, user.role)


def end_session() -> None:
    if g.get("user") is not None:
        g.user.session_epoch += 1   # 讓這個使用者所有已發出的 cookie 失效
        current_app.logger.info("logout uid=%s", g.user.id)
    session.clear()


# ---------------- CSRF ----------------
def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_protect() -> None:
    """會改資料的請求都要帶 CSRF token：表單用 hidden 欄位 csrf，API 用 X-CSRF-Token header。"""
    if request.method not in UNSAFE_METHODS or current_app.config.get("CSRF_DISABLED"):
        return
    sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, "CSRF token missing or invalid")


# ---------------- 授權 decorator ----------------
def _wants_json() -> bool:
    return request.path.startswith("/api/")


def _deny_unauthenticated():
    if _wants_json():
        abort(401)
    return redirect(url_for("main.login", next=request.full_path.rstrip("?")))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return _deny_unauthenticated()
        return view(*args, **kwargs)
    return wrapper


def role_required(*roles: str):
    """RBAC。用法：@role_required("admin")。沒登入 401／導向登入，角色不符 403。"""
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return _deny_unauthenticated()
            if g.user.role not in roles:
                current_app.logger.warning("403 role uid=%s role=%s path=%s need=%s",
                                           g.user.id, g.user.role, request.path, roles)
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco


# ---------------- ABAC ----------------
def can_view_material(user: User | None, material: Material) -> bool:
    """講義與作答紀錄只有擁有者能讀寫。

    判斷依據是「資源的屬性（owner_id）」對上「使用者的屬性（id）」，所以是 ABAC。
    admin 也不能看別人的講義：admin 的工作是管 flag 與角色，不需要讀學生資料（最小權限）。
    """
    return user is not None and material.owner_id == user.id


def get_material_or_abort(mid: int) -> Material:
    """路由共用：講義不存在 404，不是自己的 403（防 IDOR：改網址上的 id 看別人的講義）。"""
    m = current_app.extensions["store"].materials.get(mid)
    if m is None:
        abort(404)
    if not can_view_material(g.user, m):
        current_app.logger.warning("403 IDOR uid=%s material=%s owner=%s", g.user.id, mid, m.owner_id)
        abort(403)
    return m


# ---------------- 其他 ----------------
def safe_next(target: str | None) -> str:
    """登入後要跳回的網址只允許站內相對路徑，防 open redirect（/login?next=https://evil.com）。"""
    fallback = url_for("main.dashboard")
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return fallback
    parts = urlsplit(target)
    return target if not parts.scheme and not parts.netloc else fallback
