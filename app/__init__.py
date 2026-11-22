"""App 骨架（負責：陳儀珊）。

組員的模組只要提供 init_app(app)，放進 OPTIONAL_MODULES 的檔名，就會自動掛上：
- app/flags.py      賴：is_on、feature_required、Admin flag 控制台
- app/features.py   Juinn：作答、AI 解析、第 1/3/7 天提醒
- app/upload.py     林：PDF 上傳
檔案還不存在時直接略過，所以每個人可以各自合併，不會互相卡住。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, jsonify, render_template, request

from .auth import csrf_protect, csrf_token, load_user
from .store import Store

OPTIONAL_MODULES = ("app.flags", "app.features", "app.upload")


def _env_config() -> dict:
    env = os.environ.get("APP_ENV", "development")
    return dict(
        APP_ENV=env,
        SECRET_KEY=os.environ.get("SECRET_KEY") or None,
        GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID") or None,
        GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET") or None,
        ALLOWED_EMAIL_DOMAIN=os.environ.get("ALLOWED_EMAIL_DOMAIN", ""),
        ADMIN_EMAILS=os.environ.get("ADMIN_EMAILS", ""),
        BETA_TESTER_EMAILS=os.environ.get("BETA_TESTER_EMAILS", ""),
        # Session：Flask 預設的簽章 cookie
        SESSION_COOKIE_NAME="cb_session",
        SESSION_COOKIE_HTTPONLY=True,          # JavaScript 讀不到，XSS 偷不走
        SESSION_COOKIE_SAMESITE="Lax",         # 跨站 POST 不帶 cookie，再加上 CSRF token
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(_env_config())
    app.config.update(config or {})
    prod = app.config["APP_ENV"] == "production"
    if prod:
        app.config["SESSION_COOKIE_SECURE"] = True   # 正式環境 cookie 只走 HTTPS
    app.config["DEMO_LOGIN"] = not prod
    app.config["GOOGLE_ENABLED"] = bool(app.config.get("GOOGLE_CLIENT_ID"))

    # 密鑰只從環境變數（本機 .env、CI 由 GitHub Secrets 注入）來；正式環境沒設定就拒絕啟動
    if not app.config.get("SECRET_KEY"):
        if app.config["APP_ENV"] == "production":
            raise RuntimeError("SECRET_KEY must be set in production")
        app.config["SECRET_KEY"] = secrets.token_hex(32)  # 開發用：每次重啟都不同
    if app.config["APP_ENV"] == "production" and not app.config["GOOGLE_ENABLED"]:
        raise RuntimeError("GOOGLE_CLIENT_ID must be set in production (demo login is disabled)")

    logging.basicConfig(level=logging.INFO)
    app.extensions["store"] = Store()
    app.before_request(load_user)
    app.before_request(csrf_protect)
    app.after_request(_security_headers)
    app.jinja_env.globals["csrf_token"] = csrf_token
    _register_error_handlers(app)

    from .routes import bp
    app.register_blueprint(bp)

    if app.config["GOOGLE_ENABLED"]:
        from .google_auth import init_google
        init_google(app)

    for name in OPTIONAL_MODULES:
        if importlib.util.find_spec(name) is not None:
            importlib.import_module(name).init_app(app)
    return app


def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
    return resp


def _register_error_handlers(app: Flask) -> None:
    messages = {400: "請求格式錯誤", 401: "請先登入", 403: "你沒有權限做這件事",
                404: "找不到這個頁面", 405: "不支援這個操作"}

    def handle(e):
        code = getattr(e, "code", 500)
        if request.path.startswith("/api/"):
            return jsonify(error=e.name if hasattr(e, "name") else "error", status=code), code
        return render_template("error.html", code=code, message=messages.get(code, "發生錯誤")), code

    for code in messages:
        app.register_error_handler(code, handle)
