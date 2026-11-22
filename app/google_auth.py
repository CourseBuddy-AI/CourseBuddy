"""Google 登入（OAuth 2.0 Authorization Code + OIDC）（負責：陳儀珊）。

只有設定 GOOGLE_CLIENT_ID 時才會啟用；沒設定時只有開發環境的示範登入。

設定步驟：
1. Google Cloud Console → APIs & Services → Credentials → Create OAuth client ID（Web application）
2. Authorized redirect URI：http://localhost:5000/auth/google/callback（部署後再加正式網址）
3. GOOGLE_CLIENT_ID 放 .env 或 GitHub Variables；GOOGLE_CLIENT_SECRET 放 .env 或 GitHub Secrets，不要 commit
4. ALLOWED_EMAIL_DOMAIN 設成學校信箱網域（例如 mail.ntust.edu.tw），只允許校內帳號

安全重點：
- Authlib 的 authorize_access_token() 會檢查 state（防 CSRF）、nonce（防重放），並用 Google 公鑰驗 id_token 簽章、iss、aud、exp
- 使用者 ID 用 Google 的 sub（不會變、全球唯一），不用 email：email 可能改名或被回收重新註冊
- 只接受 email_verified 為 true（布林值，不是字串）；網域要完全相符，"@" 後面整段比對
- 第一次登入一律是 student；admin / beta_tester 只能由設定檔或管理員指派，使用者不能自己選
- 登入後由 start_session() 換新 session（防 session fixation）
"""
from __future__ import annotations

from flask import abort, current_app, redirect, url_for

from .auth import start_session
from .store import User

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


def _email_list(name: str) -> set[str]:
    raw = current_app.config.get(name) or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def initial_role(email: str) -> str:
    """第一次登入時的角色：預設 student；ADMIN_EMAILS / BETA_TESTER_EMAILS 是不含機密的設定。"""
    if email in _email_list("ADMIN_EMAILS"):
        return "admin"
    if email in _email_list("BETA_TESTER_EMAILS"):
        return "beta_tester"
    return "student"


def check_claims(info: dict) -> tuple[str, str]:
    """驗證 id_token 解出來的使用者資料，回傳 (sub, email)；不合格直接 403。"""
    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    if not sub or not email or info.get("email_verified") is not True:
        abort(403, "Google account email is not verified")
    domain = (current_app.config.get("ALLOWED_EMAIL_DOMAIN") or "").strip().lower()
    if domain and email.rsplit("@", 1)[-1] != domain:
        abort(403, "email domain not allowed")
    return sub, email


def init_google(app) -> None:
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(app)
    oauth.register(
        name="google",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={"scope": "openid email profile"},
    )
    app.extensions["oauth"] = oauth

    @app.get("/auth/google")
    def google_login():
        extra = {"prompt": "select_account"}
        if app.config.get("ALLOWED_EMAIL_DOMAIN"):
            extra["hd"] = app.config["ALLOWED_EMAIL_DOMAIN"]  # 只是提示 Google 顯示哪個網域，真正的檢查在 callback
        return oauth.google.authorize_redirect(url_for("google_callback", _external=True), **extra)

    @app.get("/auth/google/callback")
    def google_callback():
        try:
            token = oauth.google.authorize_access_token()
        except Exception as e:  # state 不符、code 無效、id_token 驗證失敗
            current_app.logger.warning("google login failed: %s", type(e).__name__)
            abort(401)
        sub, email = check_claims(token.get("userinfo") or {})

        store = current_app.extensions["store"]
        uid = f"google:{sub}"
        # 先找綁過這個 Google 帳號的人；再找管理員預先建立、email 相同的帳號
        user = store.users.get(uid) or store.user_by_email(email)
        if user is None:
            info = token["userinfo"]
            user = User(uid, info.get("name") or email, initial_role(email), email)
            store.users[uid] = user
        start_session(user)
        return redirect(url_for("main.dashboard"))
