"""Google OIDC 登入（Developer A）：用假的 token 模擬 Google 的回傳，測試不需要連到 Google。"""
import pytest

from app import create_app


@pytest.fixture
def gapp():
    return create_app({"TESTING": True, "SECRET_KEY": "test-only",
                       "GOOGLE_CLIENT_ID": "fake-id.apps.googleusercontent.com",
                       "GOOGLE_CLIENT_SECRET": "fake-secret",
                       "ADMIN_EMAILS": "prof@mail.ntust.edu.tw",
                       "BETA_TESTER_EMAILS": "b11233030@mail.ntust.edu.tw"})


def fake_google(app, monkeypatch, **userinfo):
    info = {"sub": "1234567890", "email": "newbie@gmail.com", "email_verified": True, "name": "Newbie"}
    info.update(userinfo)
    monkeypatch.setattr(app.extensions["oauth"].google, "authorize_access_token",
                        lambda: {"userinfo": info})


def uid_after(client):
    with client.session_transaction() as s:
        return s.get("uid")


def test_login_redirects_to_google_with_state(gapp, monkeypatch):
    monkeypatch.setattr(gapp.extensions["oauth"].google, "load_server_metadata", lambda: {
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"})
    r = gapp.test_client().get("/auth/google")
    loc = r.headers["Location"]
    assert r.status_code == 302 and loc.startswith("https://accounts.google.com/")
    assert "state=" in loc and "scope=openid+email+profile" in loc


def test_first_login_creates_student_keyed_by_sub(gapp, monkeypatch):
    fake_google(gapp, monkeypatch)
    c = gapp.test_client()
    assert c.get("/auth/google/callback").status_code == 302
    assert uid_after(c) == "google:1234567890"
    assert gapp.extensions["store"].users["google:1234567890"].role == "student"


def test_roles_from_config(gapp, monkeypatch):
    fake_google(gapp, monkeypatch, sub="1", email="B11233030@mail.ntust.edu.tw")
    c = gapp.test_client()
    c.get("/auth/google/callback")
    assert gapp.extensions["store"].users[uid_after(c)].role == "beta_tester"
    fake_google(gapp, monkeypatch, sub="2", email="prof@mail.ntust.edu.tw")
    c = gapp.test_client()
    c.get("/auth/google/callback")
    assert gapp.extensions["store"].users[uid_after(c)].role == "admin"


def test_same_local_part_does_not_take_over_account(gapp, monkeypatch):
    # 如果用 email @ 前面那段當 ID，alice@gmail.com 會登入成既有的 alice（帳號接管）
    fake_google(gapp, monkeypatch, sub="999", email="alice@gmail.com")
    c = gapp.test_client()
    c.get("/auth/google/callback")
    assert uid_after(c) == "google:999"


def test_preprovisioned_email_maps_to_existing_account(gapp, monkeypatch):
    fake_google(gapp, monkeypatch, sub="555", email="Admin@Example.edu")
    c = gapp.test_client()
    c.get("/auth/google/callback")
    assert uid_after(c) == "admin"


@pytest.mark.parametrize("claims", [
    {"email_verified": False},
    {"email_verified": "true"},   # 要是布林 True，字串不算
    {"sub": None},
    {"email": ""},
])
def test_bad_claims_rejected(gapp, monkeypatch, claims):
    fake_google(gapp, monkeypatch, **claims)
    c = gapp.test_client()
    assert c.get("/auth/google/callback").status_code == 403
    assert uid_after(c) is None


def test_domain_restriction(gapp, monkeypatch):
    gapp.config["ALLOWED_EMAIL_DOMAIN"] = "mail.ntust.edu.tw"
    fake_google(gapp, monkeypatch, email="someone@gmail.com")
    assert gapp.test_client().get("/auth/google/callback").status_code == 403
    fake_google(gapp, monkeypatch, email="evil@fakemail.ntust.edu.tw")   # 後綴相同也不行
    assert gapp.test_client().get("/auth/google/callback").status_code == 403
    fake_google(gapp, monkeypatch, email="b11233030@mail.ntust.edu.tw")
    assert gapp.test_client().get("/auth/google/callback").status_code == 302


def test_invalid_state_or_token_is_401(gapp, monkeypatch):
    def boom():
        raise ValueError("mismatching_state")
    monkeypatch.setattr(gapp.extensions["oauth"].google, "authorize_access_token", boom)
    assert gapp.test_client().get("/auth/google/callback").status_code == 401


def test_google_routes_absent_when_not_configured(client):
    assert client.get("/auth/google").status_code == 404
