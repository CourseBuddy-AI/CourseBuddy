"""登入、首頁、講義頁、管理員角色指派（負責：陳儀珊）。

其他組員的路由放在自己的檔案（flags.py、features.py、upload.py），用 init_app(app) 掛上來，
不用改這個檔案。
"""
from __future__ import annotations

from flask import (Blueprint, abort, current_app, g, jsonify, redirect, render_template,
                   request, url_for)

from .auth import (end_session, get_material_or_abort, login_required, role_required,
                   safe_next, start_session)
from .store import ROLES

bp = Blueprint("main", __name__)


def store():
    return current_app.extensions["store"]


# ---------------- 登入 / 登出 ----------------
@bp.get("/login")
def login():
    if g.user is not None:
        return redirect(url_for("main.dashboard"))
    return render_template("login.html",
                           demo=current_app.config["DEMO_LOGIN"],
                           google=current_app.config["GOOGLE_ENABLED"],
                           users=store().users.values() if current_app.config["DEMO_LOGIN"] else [])


@bp.post("/login")
def login_post():
    """示範登入：只在開發 / 測試環境開放，正式環境 404，一律走 Google OIDC。"""
    if not current_app.config["DEMO_LOGIN"]:
        abort(404)
    user = store().users.get(request.form.get("uid", ""))
    if user is None:
        abort(400)
    start_session(user)
    return redirect(safe_next(request.args.get("next")))


@bp.post("/logout")
def logout():
    end_session()
    return redirect(url_for("main.login"))


# ---------------- 首頁、講義 ----------------
@bp.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html", materials=store().materials_of(g.user.id))


@bp.get("/materials/<int:mid>")
@login_required
def material(mid: int):
    m = get_material_or_abort(mid)
    return render_template("material.html", m=m)


# ---------------- JSON API ----------------
@bp.get("/api/me")
@login_required
def api_me():
    return jsonify(id=g.user.id, name=g.user.name, role=g.user.role)


@bp.get("/api/materials/<int:mid>")
@login_required
def api_material(mid: int):
    m = get_material_or_abort(mid)
    return jsonify(id=m.id, title=m.title, owner_id=m.owner_id, pages=len(m.pages))


# ---------------- 管理員：指派角色（RBAC） ----------------
@bp.get("/admin/users")
@role_required("admin")
def admin_users():
    return render_template("admin_users.html", users=store().users.values(), roles=ROLES)


@bp.post("/admin/users/<uid>/role")
@role_required("admin")
def admin_set_role(uid: str):
    role = request.form.get("role", "")
    if uid not in store().users:
        abort(404)
    if role not in ROLES:
        abort(400)
    if uid == g.user.id and role != "admin":
        abort(400, "admin cannot demote themselves")  # 避免系統沒有任何 admin
    old = store().users[uid].role
    store().set_role(uid, role)
    current_app.logger.warning("role change uid=%s %s -> %s by %s", uid, old, role, g.user.id)
    return redirect(url_for("main.admin_users"))


@bp.get("/healthz")
def healthz():
    return {"status": "ok"}
