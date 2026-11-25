"""角色設定與角色權限（Developer A，issue #19）。

1. 沒登入打受保護 API → 401（掃過所有 /api/* 路由，之後新增的 API 忘了加 login_required 也會被抓到）
2. 角色不對 → 403（student、beta_tester 打所有 /admin/* 路由）
3. 第一次 Google 登入的角色：ADMIN_EMAILS → admin、BETA_TESTER_EMAILS → beta_tester、其他 → student
"""
import json
import re
from pathlib import Path

import pytest

from app import create_app
from app.google_auth import initial_role

ADMIN_EMAIL = "prof@mail.ntust.edu.tw"
BETA_EMAIL = "b11233030@mail.ntust.edu.tw"

# 不需要登入的 API（目前沒有）。要新增公開 API 時，加到這裡並在 PR 說明理由。
PUBLIC_API: set[str] = set()


REPO_FLAGS = Path(__file__).resolve().parent.parent / "flags.json"


@pytest.fixture
def flags_file(tmp_path):
    """flags.json 的暫存複本：就算權限檢查壞掉、請求被放行，也不會改到 repo 裡的 flags.json。

    兩個 flag 都先打開，這樣「被擋下的 kill 請求沒有副作用」才檢查得出來。
    """
    data = json.loads(REPO_FLAGS.read_text(encoding="utf-8"))
    for name in ("ai_quiz", "spaced_reminder"):
        data[name].update(enabled=True, rollout=100)
    p = tmp_path / "flags.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def app(flags_file):
    # 覆寫 conftest 的 app：這個檔案的測試（包含 login fixture）都用暫存的 flags.json
    return create_app({"TESTING": True, "SECRET_KEY": "test-only", "APP_ENV": "development",
                       "FLAGS_FILE": str(flags_file)})


def _concrete(rule: str) -> str:
    """把 /api/materials/<int:mid> 換成 /api/materials/1，才打得到。"""
    return re.sub(r"<[^>]+>", "1", rule)


def _rules(app, prefix):
    out = []
    for r in app.url_map.iter_rules():
        if r.rule.startswith(prefix):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                out.append((m, r.rule))
    return sorted(out)


_ROUTE_APP = create_app({"TESTING": True, "SECRET_KEY": "test-only"})
API_ROUTES = [x for x in _rules(_ROUTE_APP, "/api/") if x[1] not in PUBLIC_API]
ADMIN_ROUTES = _rules(_ROUTE_APP, "/admin/")


def _anon_with_csrf(app):
    """未登入但有合法 CSRF token 的 client：確保 POST 拿到的是 401，而不是被 CSRF 先擋成 400。"""
    c = app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        return c, s["csrf"]


# ---------- 1. 沒登入 → 401 ----------
def test_route_lists_not_empty():
    # 防呆：路由掃描壞掉時，下面的 parametrize 會變成 0 個測試而默默通過
    assert len(API_ROUTES) >= 5 and len(ADMIN_ROUTES) >= 5


@pytest.mark.parametrize("method,rule", API_ROUTES)
def test_every_api_without_login_is_401(app, method, rule):
    c, token = _anon_with_csrf(app)
    r = c.open(_concrete(rule), method=method, headers={"X-CSRF-Token": token})
    assert r.status_code == 401, f"{method} {rule} 沒登入也能打（{r.status_code}）"
    assert r.get_json() == {"error": "Unauthorized", "status": 401}


@pytest.mark.parametrize("method,rule", ADMIN_ROUTES)
def test_admin_routes_without_login_go_to_login(app, method, rule):
    c, token = _anon_with_csrf(app)
    r = c.open(_concrete(rule), method=method, data={"csrf": token})
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")


def test_logged_out_cookie_is_401(login):
    alice = login("alice")
    assert alice.get("/api/me").status_code == 200
    alice.post("/logout")
    assert alice.get("/api/me").status_code == 401


# ---------- 2. 角色不對 → 403 ----------
@pytest.mark.parametrize("uid", ["alice", "betty"])   # student、beta_tester
@pytest.mark.parametrize("method,rule", ADMIN_ROUTES)
def test_non_admin_on_admin_route_is_403(login, uid, method, rule):
    u = login(uid)
    r = u.post(_concrete(rule)) if method == "POST" else u.get(_concrete(rule))
    assert r.status_code == 403, f"{uid} 打 {method} {rule} 得到 {r.status_code}"


@pytest.mark.parametrize("url", ["/admin/users", "/admin/flags", "/admin/flags/metrics.json"])
def test_admin_can_open_admin_pages(login, url):
    assert login("admin").get(url).status_code == 200


def test_403_does_not_change_anything(app, login, flags_file):
    # 被擋下的請求不能有副作用：角色、flag 都維持原樣
    store = app.extensions["store"]
    flags = app.extensions["flags"]
    file_before = flags_file.read_bytes()
    flags_before = flags.all()
    assert flags_before["ai_quiz"].enabled   # 先確認 flag 是開的，kill 被放行的話才看得出差別

    for uid in ("alice", "betty"):
        u = login(uid)
        assert u.post(f"/admin/users/{uid}/role", data={"role": "admin"}).status_code == 403
        assert u.post("/admin/flags/kill-all").status_code == 403
        assert u.post("/admin/flags/ai_quiz/kill").status_code == 403
        assert u.post("/admin/flags/ai_quiz", data={"enabled": "false"}).status_code == 403

    assert store.users["alice"].role == "student"
    assert store.users["betty"].role == "beta_tester"
    assert flags_file.read_bytes() == file_before
    assert flags.all() == flags_before
    assert flags.audit == []


# ---------- 3. 第一次登入的角色 ----------
@pytest.fixture
def gapp(flags_file):
    return create_app({"TESTING": True, "SECRET_KEY": "test-only",
                       "GOOGLE_CLIENT_ID": "fake-id.apps.googleusercontent.com",
                       "GOOGLE_CLIENT_SECRET": "fake-secret",
                       "FLAGS_FILE": str(flags_file),
                       "ADMIN_EMAILS": f" {ADMIN_EMAIL.upper()} , ta@mail.ntust.edu.tw,",
                       "BETA_TESTER_EMAILS": BETA_EMAIL,
                       })


def google_login(app, monkeypatch, sub, email):
    """模擬 Google 回傳，登入後回傳 (client, user)。"""
    info = {"sub": sub, "email": email, "email_verified": True, "name": email}
    monkeypatch.setattr(app.extensions["oauth"].google, "authorize_access_token",
                        lambda: {"userinfo": info})
    c = app.test_client()
    assert c.get("/auth/google/callback").status_code == 302
    with c.session_transaction() as s:
        uid = s["uid"]
    return c, app.extensions["store"].users[uid]


@pytest.mark.parametrize("sub,email,role", [
    ("1", ADMIN_EMAIL, "admin"),
    ("2", "ta@mail.ntust.edu.tw", "admin"),          # 清單裡第二個
    ("3", BETA_EMAIL.upper(), "beta_tester"),        # 不分大小寫
    ("4", "classmate@mail.ntust.edu.tw", "student"),  # 不在任何清單
])
def test_first_login_role(gapp, monkeypatch, sub, email, role):
    _, user = google_login(gapp, monkeypatch, sub, email)
    assert user.role == role


def test_role_takes_effect_on_server(gapp, monkeypatch):
    # 不只 store 裡的欄位對，實際打 admin 頁面的結果也要對
    admin, _ = google_login(gapp, monkeypatch, "1", ADMIN_EMAIL)
    beta, _ = google_login(gapp, monkeypatch, "3", BETA_EMAIL)
    student, _ = google_login(gapp, monkeypatch, "4", "classmate@mail.ntust.edu.tw")
    assert admin.get("/api/me").get_json()["role"] == "admin"
    assert admin.get("/admin/users").status_code == 200
    assert beta.get("/api/me").get_json()["role"] == "beta_tester"
    assert beta.get("/admin/users").status_code == 403
    assert student.get("/admin/users").status_code == 403


def test_role_only_set_on_first_login(gapp, monkeypatch):
    # admin 之後在後台改的角色，重新登入不會被設定檔蓋回去
    _, user = google_login(gapp, monkeypatch, "1", ADMIN_EMAIL)
    gapp.extensions["store"].set_role(user.id, "student")
    _, again = google_login(gapp, monkeypatch, "1", ADMIN_EMAIL)
    assert again is user and again.role == "student"


def test_lookalike_email_is_not_admin(gapp, monkeypatch):
    # 子字串、前後綴不能算：整個 email 要完全相同
    for sub, email in [("10", "xprof@mail.ntust.edu.tw"),
                       ("11", "prof@mail.ntust.edu.tw.evil.com"),
                       ("12", "prof@mail.ntust.edu")]:
        _, user = google_login(gapp, monkeypatch, sub, email)
        assert user.role == "student", email


@pytest.mark.parametrize("admins,betas,email,role", [
    ("", "", ADMIN_EMAIL, "student"),                 # 沒設定時所有人都是 student
    ("a@x.edu", "a@x.edu", "a@x.edu", "admin"),       # 兩邊都有 → admin 優先
    (",, ,", ",", "", "student"),                     # 空字串不會因為空項目被當成 admin
])
def test_initial_role_edge_cases(admins, betas, email, role):
    app = create_app({"TESTING": True, "SECRET_KEY": "x",
                      "ADMIN_EMAILS": admins, "BETA_TESTER_EMAILS": betas})
    with app.app_context():
        assert initial_role(email) == role


def test_env_example_documents_role_settings():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line and not line.startswith("#")}
    assert {"ADMIN_EMAILS", "BETA_TESTER_EMAILS"} <= keys
