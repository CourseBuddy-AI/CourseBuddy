"""伺服器端存取控制（Developer A）：認證 401、IDOR 403、RBAC 403、CSRF、open redirect。

這些測試都繞過畫面，直接打後端，證明「藏按鈕」之外伺服器也有擋。
"""
import pytest

from app import create_app

ALICE_MAT = 1   # alice 的講義
BOB_MAT = 2     # bob 的私人筆記
BETTY_MAT = 3   # betty（beta_tester）的講義


# ---------- 認證：未登入 ----------
@pytest.mark.parametrize("url", ["/api/me", f"/api/materials/{ALICE_MAT}"])
def test_api_without_login_is_401(client, url):
    r = client.get(url)
    assert r.status_code == 401
    assert r.get_json()["status"] == 401


@pytest.mark.parametrize("url", ["/", f"/materials/{ALICE_MAT}", "/admin/users"])
def test_pages_without_login_redirect_to_login(client, url):
    r = client.get(url)
    assert r.status_code == 302
    assert r.headers["Location"].startswith("/login")


def test_forged_cookie_is_rejected(app):
    # 用別的 SECRET_KEY 簽出來的 cookie，伺服器驗簽失敗 → 當作沒登入
    other = create_app({"TESTING": True, "SECRET_KEY": "attacker-key"})
    c = other.test_client()
    with c.session_transaction() as s:
        s["uid"], s["epoch"] = "admin", 0
    forged = c.get_cookie("cb_session").value
    victim = app.test_client()
    victim.set_cookie("cb_session", forged)
    assert victim.get("/api/me").status_code == 401


# ---------- ABAC：只能看自己的講義（IDOR） ----------
def test_owner_can_view_own_material(login):
    alice = login("alice")
    assert alice.get(f"/materials/{ALICE_MAT}").status_code == 200
    assert alice.get(f"/api/materials/{ALICE_MAT}").get_json()["owner_id"] == "alice"


@pytest.mark.parametrize("uid,mid", [("alice", BOB_MAT), ("bob", ALICE_MAT), ("betty", ALICE_MAT)])
def test_idor_other_users_material_is_403(login, uid, mid):
    u = login(uid)
    assert u.get(f"/materials/{mid}").status_code == 403
    assert u.get(f"/api/materials/{mid}").status_code == 403


def test_admin_cannot_read_students_material(login):
    # 最小權限：admin 管 flag 與角色，不需要讀學生講義
    assert login("admin").get(f"/api/materials/{ALICE_MAT}").status_code == 403


def test_missing_material_is_404(login):
    assert login("alice").get("/api/materials/999").status_code == 404


def test_dashboard_lists_only_own_materials(login):
    page = login("alice").get("/").get_data(as_text=True)
    assert "AI 導論" in page and "Bob 的私人筆記" not in page


# ---------- RBAC：只有 admin 能改角色、改 flag ----------
@pytest.mark.parametrize("uid,code", [("alice", 403), ("betty", 403), ("admin", 200)])
def test_admin_users_page_matrix(login, uid, code):
    assert login(uid).get("/admin/users").status_code == code


@pytest.mark.parametrize("uid", ["alice", "betty"])
def test_non_admin_cannot_change_role(app, login, uid):
    # 學生想把自己升成 admin 或 beta_tester（直接打 API，不經過畫面）
    r = login(uid).post(f"/admin/users/{uid}/role", data={"role": "admin"})
    assert r.status_code == 403
    assert app.extensions["store"].users[uid].role != "admin"


def test_admin_assigns_beta_tester_and_it_applies_next_request(app, login):
    alice = login("alice")
    assert alice.get("/api/me").get_json()["role"] == "student"
    r = login("admin").post("/admin/users/alice/role", data={"role": "beta_tester"})
    assert r.status_code == 302
    # session 只存 uid，角色每次從 store 讀：不用重新登入就生效
    assert alice.get("/api/me").get_json()["role"] == "beta_tester"


def test_admin_role_validation(login):
    admin = login("admin")
    assert admin.post("/admin/users/alice/role", data={"role": "superuser"}).status_code == 400
    assert admin.post("/admin/users/nobody/role", data={"role": "student"}).status_code == 404
    assert admin.post("/admin/users/admin/role", data={"role": "student"}).status_code == 400


@pytest.mark.parametrize("uid", ["alice", "betty"])
def test_non_admin_cannot_change_flag(app, login, uid):
    """賴的 flag 控制台（POST /admin/flags/<name>）也要用 role_required("admin")。"""
    rules = {r.rule for r in app.url_map.iter_rules()}
    if "/admin/flags/<name>" not in rules:
        pytest.skip("flag 控制台還沒合併（賴的 flags.py）")
    r = login(uid).post("/admin/flags/ai_quiz", data={"enabled": "false"})
    assert r.status_code == 403


def test_role_required_on_any_route(app):
    """role_required 本身的行為：沒登入 401、角色不符 403、符合 200。"""
    from flask import Blueprint

    from app.auth import role_required
    from tests.conftest import As

    bp = Blueprint("probe", __name__)

    @bp.post("/api/probe/admin-only")
    @role_required("admin")
    def admin_only():
        return {"ok": True}

    app.register_blueprint(bp)
    anon = app.test_client()
    anon.get("/login")
    with anon.session_transaction() as s:
        token = s["csrf"]
    assert anon.post("/api/probe/admin-only", headers={"X-CSRF-Token": token}).status_code == 401
    assert As(app.test_client(), "betty").api_post("/api/probe/admin-only").status_code == 403
    assert As(app.test_client(), "admin").api_post("/api/probe/admin-only").status_code == 200


# ---------- CSRF ----------
def test_post_without_csrf_is_400(login):
    admin = login("admin")
    r = admin.c.post("/admin/users/alice/role", data={"role": "beta_tester"})
    assert r.status_code == 400


def test_post_with_wrong_csrf_is_400(login):
    admin = login("admin")
    r = admin.c.post("/admin/users/alice/role", data={"role": "beta_tester", "csrf": "guess"})
    assert r.status_code == 400


# ---------- 示範登入只在開發環境 ----------
def test_demo_login_disabled_in_production():
    app = create_app({"APP_ENV": "production", "SECRET_KEY": "x", "GOOGLE_CLIENT_ID": "id",
                      "GOOGLE_CLIENT_SECRET": "s", "CSRF_DISABLED": True})
    assert app.test_client().post("/login", data={"uid": "admin"}).status_code == 404


def test_production_requires_secret_key(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"APP_ENV": "production", "SECRET_KEY": None, "GOOGLE_CLIENT_ID": "id"})


def test_production_requires_google_login():
    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        create_app({"APP_ENV": "production", "SECRET_KEY": "x", "GOOGLE_CLIENT_ID": None})


def test_unknown_demo_user_is_400(client):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    assert client.post("/login", data={"uid": "ghost", "csrf": token}).status_code == 400


# ---------- Open redirect ----------
@pytest.mark.parametrize("nxt", ["https://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)"])
def test_login_next_cannot_redirect_offsite(client, nxt):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    r = client.post("/login", query_string={"next": nxt}, data={"uid": "alice", "csrf": token})
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_login_next_keeps_internal_path(client):
    client.get("/login")
    with client.session_transaction() as s:
        token = s["csrf"]
    r = client.post("/login", query_string={"next": "/materials/1"}, data={"uid": "alice", "csrf": token})
    assert r.headers["Location"] == "/materials/1"
