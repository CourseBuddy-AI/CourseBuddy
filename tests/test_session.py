"""Session（Developer A）：cookie 屬性、session fixation、登出後舊 cookie 失效。"""
from datetime import timedelta

from app import create_app


def test_cookie_flags(login):
    alice = login("alice")
    ck = alice.c.get_cookie("cb_session")
    assert ck.http_only
    assert ck.same_site == "Lax"


def test_cookie_secure_in_production():
    app = create_app({"APP_ENV": "production", "SECRET_KEY": "x", "GOOGLE_CLIENT_ID": "id",
                      "GOOGLE_CLIENT_SECRET": "s"})
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["PERMANENT_SESSION_LIFETIME"] == timedelta(hours=8)


def test_session_fixation_new_session_on_login(client):
    client.get("/login")
    with client.session_transaction() as s:
        s["planted"] = "attacker"          # 攻擊者事先塞給受害者的 session 內容
        token = s["csrf"]
    client.post("/login", data={"uid": "alice", "csrf": token})
    with client.session_transaction() as s:
        assert "planted" not in s
        assert s["uid"] == "alice"
        assert s["csrf"] != token          # CSRF token 也換新


def test_session_stores_only_uid_not_role(login):
    with login("alice").c.session_transaction() as s:
        assert "role" not in s             # 角色不放 cookie，改角色或降權立刻生效


def test_logout_invalidates_copied_cookie(app, login):
    alice = login("alice")
    stolen = alice.c.get_cookie("cb_session").value      # 假設 cookie 在登出前被複製
    assert alice.post("/logout").status_code == 302
    attacker = app.test_client()
    attacker.set_cookie("cb_session", stolen)
    assert attacker.get("/api/me").status_code == 401    # epoch 已經 +1，舊 cookie 失效


def test_expired_session_is_rejected(app, login):
    alice = login("alice")
    app.permanent_session_lifetime = timedelta(seconds=-1)   # 模擬 8 小時已過
    assert alice.get("/api/me").status_code == 401


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
