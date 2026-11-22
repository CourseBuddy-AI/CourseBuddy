"""Feature flag 測試（負責：賴 / Operator・QA）：TC1–TC7 ＋ 熱重載、kill switch、監控。

每個測試都用自己的 flags.json 複本（tmp_path），不會改到 repo 裡的檔案。

功能本身（作答、AI 解析、提醒）在 Juinn 的 features.py、畫面在張的 material.html。
為了讓 flag 系統不用等他們就能驗證，這裡掛一組 probe 路由，用的是和正式功能一模一樣的介面：
  - 畫面：{% if flag_on("ai_quiz") %}<section data-flag="ai_quiz">…{% endif %}
  - API ：@feature_required("ai_quiz")
  - 排程：送出前 still_on("spaced_reminder", user)
他們的程式合併後，TC1 的「真實畫面」測試會自動改測 /materials/<id>。
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest
from flask import Blueprint, g, render_template_string

from app import create_app
from app.flags import KNOWN_FLAGS, bucket, enabled_for, feature_required, is_on, parse_flags, still_on
from tests.conftest import As

REPO_FLAGS = Path(__file__).resolve().parent.parent / "flags.json"
BETA = ("betty", "ben")          # 示範帳號裡的 beta_tester
FLAGS = KNOWN_FLAGS              # ("ai_quiz", "spaced_reminder")

PROBE_PAGE = """
<h1>講義</h1>
<section class="core">作答與錯題本（核心功能，一直開著）</section>
{% if flag_on("ai_quiz") %}<section data-flag="ai_quiz">AI 解析：出自講義第 2 頁</section>
{% else %}<section class="answer-only">正確答案：B</section>{% endif %}
{% if flag_on("spaced_reminder") %}<section data-flag="spaced_reminder">第 1、3、7 天提醒你回來重做</section>{% endif %}
"""


# ---------------- 測試工具 ----------------
def write_flags(path: Path, **overrides) -> None:
    """模擬 Operator 手動改 flags.json 存檔（不是透過控制台）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    for name, changes in overrides.items():
        data[name].update(changes)
    before = path.stat().st_mtime_ns
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.stat().st_mtime_ns == before:          # 檔案系統時間解析度太粗時，確保 mtime 有變
        os.utime(path, ns=(before + 1_000_000, before + 1_000_000))


@pytest.fixture
def flags_file(tmp_path):
    p = tmp_path / "flags.json"
    p.write_text(REPO_FLAGS.read_text(encoding="utf-8"), encoding="utf-8")
    write_flags(p, ai_quiz={"enabled": False, "rollout": 0, "allow": []},
                spaced_reminder={"enabled": False, "rollout": 0, "allow": []})
    return p


@pytest.fixture
def fapp(flags_file):
    app = create_app({"TESTING": True, "SECRET_KEY": "test-only", "APP_ENV": "development",
                      "FLAGS_FILE": str(flags_file)})
    probe = Blueprint("probe_flags", __name__)

    @probe.get("/probe/material")
    def page():
        from app.auth import login_required
        return login_required(lambda: render_template_string(PROBE_PAGE))()

    @probe.get("/api/probe/explain")
    @feature_required("ai_quiz")
    def explain():
        return {"explanation": "過擬合是…", "page": 3}

    @probe.post("/api/probe/reminders")
    @feature_required("spaced_reminder")
    def schedule():
        return {"scheduled": [1, 3, 7]}

    @probe.get("/api/probe/boom")
    @feature_required("ai_quiz")
    def boom():
        raise RuntimeError("LLM parse failed")

    app.register_blueprint(probe)
    return app


@pytest.fixture
def as_(fapp):
    return lambda uid: As(fapp.test_client(), uid)


def store(app):
    return app.extensions["store"]


def summary(app):
    with app.app_context():
        from app import metrics
        return metrics.summary()


# =====================================================================
# TC1 flag OFF：畫面看不到功能
# =====================================================================
@pytest.mark.parametrize("uid", BETA)
def test_tc1_flag_off_page_hides_features(as_, uid):
    html = as_(uid).get("/probe/material").get_data(as_text=True)
    assert "作答與錯題本" in html                       # 核心功能還在
    assert 'data-flag="ai_quiz"' not in html and "AI 解析" not in html
    assert 'data-flag="spaced_reminder"' not in html
    assert "正確答案：B" in html                         # OFF 時答錯只顯示正確答案


@pytest.mark.parametrize("flag", FLAGS)
def test_tc1_api_flags_reports_off(as_, flag):
    assert as_("betty").get("/api/flags").get_json()[flag] is False


def test_tc1_real_material_page_when_features_merged(fapp, flags_file, as_):
    """張的 ON/OFF 區塊合併後（有 data-flag 標記）自動生效；還沒合併時 skip。"""
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100},
                spaced_reminder={"enabled": True, "rollout": 100})
    betty = as_("betty")
    on_html = betty.get("/materials/3").get_data(as_text=True)
    if "data-flag=" not in on_html:
        pytest.skip("material.html 還沒有 data-flag 區塊（張 / Juinn 的 PR 尚未合併）")
    write_flags(flags_file, ai_quiz={"enabled": False}, spaced_reminder={"enabled": False})
    off_html = betty.get("/materials/3").get_data(as_text=True)
    for flag in FLAGS:
        assert f'data-flag="{flag}"' in on_html
        assert f'data-flag="{flag}"' not in off_html


# =====================================================================
# TC2 flag OFF：直接打 API 回 403（藏按鈕不算存取控制）
# =====================================================================
@pytest.mark.parametrize("uid", BETA)
def test_tc2_flag_off_api_is_403(fapp, as_, uid):
    u = as_(uid)
    r = u.get("/api/probe/explain")
    assert r.status_code == 403
    assert r.get_json() == {"error": "Forbidden", "status": 403}
    assert u.api_post("/api/probe/reminders").status_code == 403
    s = summary(fapp)
    assert s["ai_quiz"]["off"]["blocked"] >= 1          # 被擋的呼叫有記到監控
    assert s["spaced_reminder"]["off"]["blocked"] >= 1


def test_tc2_not_logged_in_is_401_not_403(fapp):
    assert fapp.test_client().get("/api/probe/explain").status_code == 401


# =====================================================================
# TC3 flag ON、rollout=100：所有 beta_tester 看得到
# =====================================================================
def test_tc3_rollout_100_all_beta_testers_see_features(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100},
                spaced_reminder={"enabled": True, "rollout": 100})
    betas = [u for u in store(fapp).users.values() if u.role == "beta_tester"]
    assert {u.id for u in betas} == set(BETA)
    for u in betas:
        c = as_(u.id)
        html = c.get("/probe/material").get_data(as_text=True)
        assert 'data-flag="ai_quiz"' in html and 'data-flag="spaced_reminder"' in html
        assert c.get("/api/probe/explain").status_code == 200
        assert c.api_post("/api/probe/reminders").status_code == 200
        assert c.get("/api/flags").get_json() == {"ai_quiz": True, "spaced_reminder": True}
    s = summary(fapp)
    assert s["ai_quiz"]["on"]["events"]["request"] == len(betas)


def test_tc3_rollout_100_for_many_beta_testers(fapp, flags_file):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100})
    with fapp.app_context():
        assert all(is_on("ai_quiz", f"beta{i}") for i in range(1000))


# =====================================================================
# TC4 rollout=50：同一 user_id 結果都一樣；1000 個 user_id 約 50% 為 ON
# =====================================================================
@pytest.mark.parametrize("flag", FLAGS)
def test_tc4_rollout_50_is_sticky_and_about_half(fapp, flags_file, flag):
    write_flags(flags_file, **{flag: {"enabled": True, "rollout": 50}})
    ids = [f"user{i:04d}" for i in range(1000)]
    with fapp.app_context():
        first = {uid: is_on(flag, uid) for uid in ids}
        for _ in range(5):                                   # 重複問：結果完全一樣
            assert {uid: is_on(flag, uid) for uid in ids} == first
    on = sum(first.values())
    print(f"\nTC4 {flag}: {on}/1000 ON ({on / 10:.1f}%)")
    assert 450 <= on <= 550


def test_tc4_sticky_across_restart(flags_file):
    """重啟（新的 app）後分組不變：分組只由 sha256 決定，不靠記憶體或 session。"""
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 50})
    results = []
    for _ in range(2):
        app = create_app({"TESTING": True, "SECRET_KEY": "x", "FLAGS_FILE": str(flags_file)})
        with app.app_context():
            results.append([is_on("ai_quiz", f"user{i}") for i in range(200)])
    assert results[0] == results[1]


def test_tc4_bucket_is_sha256_of_flag_and_user():
    import hashlib
    expected = int(hashlib.sha256(b"ai_quiz:betty").hexdigest(), 16) % 100
    assert bucket("ai_quiz", "betty") == expected
    assert all(0 <= bucket("ai_quiz", f"u{i}") < 100 for i in range(500))


def test_tc4_increasing_rollout_never_kicks_users_out(fapp, flags_file):
    """5% → 50% → 100%：之前在 ON 的人一定還在 ON（使用者不會忽開忽關）。"""
    ids = [f"user{i}" for i in range(1000)]
    prev: set[str] = set()
    with fapp.app_context():
        for pct in (5, 50, 100):
            write_flags(flags_file, ai_quiz={"enabled": True, "rollout": pct})
            now = {u for u in ids if is_on("ai_quiz", u)}
            assert prev <= now
            prev = now
    assert len(prev) == 1000


def test_tc4_two_flags_split_independently(fapp, flags_file):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 50},
                spaced_reminder={"enabled": True, "rollout": 50})
    ids = [f"user{i}" for i in range(1000)]
    with fapp.app_context():
        both = sum(is_on("ai_quiz", u) and is_on("spaced_reminder", u) for u in ids)
    assert 200 <= both <= 300            # 獨立的話約 25% 兩個都 ON，而不是 50%


# =====================================================================
# TC5 切換：ON → OFF 下一個請求立即生效，不用重啟；已排程的提醒發送前再檢查
# =====================================================================
def test_tc5_edit_file_on_to_off_next_request_no_restart(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100})
    betty = as_("betty")
    assert betty.get("/api/probe/explain").status_code == 200
    app_id = id(fapp)

    write_flags(flags_file, ai_quiz={"enabled": False})         # Operator 改檔存檔
    t0 = time.perf_counter()
    r = betty.get("/api/probe/explain")                          # 下一個請求
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert r.status_code == 403
    assert "AI 解析" not in betty.get("/probe/material").get_data(as_text=True)
    assert id(fapp) == app_id                                    # 同一個 app，沒有重啟
    print(f"\nTC5 ON→OFF took effect on the next request ({elapsed_ms:.1f} ms)")

    write_flags(flags_file, ai_quiz={"enabled": True})          # OFF → ON 也一樣
    assert betty.get("/api/probe/explain").status_code == 200


def test_tc5_console_kill_switch_next_request(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100},
                spaced_reminder={"enabled": True, "rollout": 100})
    betty, admin = as_("betty"), as_("admin")
    assert betty.get("/api/probe/explain").status_code == 200

    r = admin.post("/admin/flags/ai_quiz/kill", data={"reason": "錯誤率暴增"})
    assert r.status_code == 302
    assert betty.get("/api/probe/explain").status_code == 403
    assert betty.api_post("/api/probe/reminders").status_code == 200      # 只關 ai_quiz

    saved = json.loads(flags_file.read_text(encoding="utf-8"))
    assert saved["ai_quiz"]["enabled"] is False                         # 寫回檔案：其他 worker 也會生效
    assert saved["ai_quiz"]["updated_by"] == "admin:admin"              # 誰按的
    assert fapp.extensions["flags"].audit[0]["reason"] == "錯誤率暴增"


def test_tc5_kill_all_one_click(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100},
                spaced_reminder={"enabled": True, "rollout": 100})
    betty = as_("betty")
    assert as_("admin").post("/admin/flags/kill-all", data={"reason": "LLM 成本超標"}).status_code == 302
    assert betty.get("/api/probe/explain").status_code == 403
    assert betty.api_post("/api/probe/reminders").status_code == 403
    assert betty.get("/api/flags").get_json() == {"ai_quiz": False, "spaced_reminder": False}


def test_tc5_scheduled_reminders_recheck_flag_before_sending(fapp, flags_file):
    """排程時 ON 的提醒，發送前 flag 被關掉 → 一封都不送，並記成 suppressed。"""
    write_flags(flags_file, spaced_reminder={"enabled": True, "rollout": 100})
    users = store(fapp).users
    with fapp.test_request_context():
        g.user = None
        queue = [(users[uid], day) for uid in BETA for day in (1, 3, 7)]
        assert all(enabled_for("spaced_reminder", u) for u, _ in queue)   # 排程當下是 ON

        write_flags(flags_file, spaced_reminder={"enabled": False})       # 第 1 天前被關掉
        sent = [(u.id, d) for u, d in queue if still_on("spaced_reminder", u)]
        assert sent == []
    assert summary(fapp)["spaced_reminder"]["off"]["events"]["suppressed"] == len(queue)


def test_tc5_scheduled_reminders_still_sent_when_on(fapp, flags_file):
    write_flags(flags_file, spaced_reminder={"enabled": True, "rollout": 100})
    with fapp.app_context():
        u = store(fapp).users["betty"]
        assert still_on("spaced_reminder", u)


# =====================================================================
# TC6 非 beta_tester（student）：不會被分到實驗組
# =====================================================================
@pytest.mark.parametrize("uid", ["alice", "bob", "admin"])
def test_tc6_non_beta_never_in_experiment(fapp, flags_file, as_, uid):
    write_flags(flags_file,
                ai_quiz={"enabled": True, "rollout": 100, "allow": [uid]},          # 連名單都沒用
                spaced_reminder={"enabled": True, "rollout": 100, "allow": [uid]})
    c = as_(uid)
    html = c.get("/probe/material").get_data(as_text=True)
    assert "data-flag=" not in html
    assert c.get("/api/probe/explain").status_code == 403
    assert c.api_post("/api/probe/reminders").status_code == 403
    assert c.get("/api/flags").get_json() == {"ai_quiz": False, "spaced_reminder": False}
    s = summary(fapp)
    assert s["ai_quiz"]["excluded"]["blocked"] == 1      # 算在「不在實驗」，不汙染對照組
    assert s["ai_quiz"]["off"]["blocked"] == 0


def test_tc6_promoted_to_beta_tester_joins_next_request(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100})
    alice = as_("alice")
    assert alice.get("/api/probe/explain").status_code == 403
    as_("admin").post("/admin/users/alice/role", data={"role": "beta_tester"})
    assert alice.get("/api/probe/explain").status_code == 200


# =====================================================================
# TC7 非 admin 改 flag：403
# =====================================================================
@pytest.mark.parametrize("uid", ["alice", "betty"])
@pytest.mark.parametrize("method,url,data", [
    ("get", "/admin/flags", None),
    ("get", "/admin/flags/metrics.json", None),
    ("post", "/admin/flags/ai_quiz", {"enabled": "true", "rollout": "100"}),
    ("post", "/admin/flags/ai_quiz/kill", {}),
    ("post", "/admin/flags/kill-all", {}),
])
def test_tc7_non_admin_cannot_touch_flags(fapp, flags_file, as_, uid, method, url, data):
    before = flags_file.read_text(encoding="utf-8")
    c = as_(uid)
    r = c.get(url) if method == "get" else c.post(url, data=data)
    assert r.status_code == 403
    assert flags_file.read_text(encoding="utf-8") == before        # 檔案沒被改


def test_tc7_not_logged_in_redirected_to_login(fapp):
    r = fapp.test_client().get("/admin/flags")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")


def test_tc7_admin_can_change_flag(fapp, flags_file, as_):
    admin = as_("admin")
    assert admin.get("/admin/flags").status_code == 200
    r = admin.post("/admin/flags/ai_quiz", data={"enabled": "true", "rollout": "50"})
    assert r.status_code == 302
    saved = json.loads(flags_file.read_text(encoding="utf-8"))["ai_quiz"]
    assert saved["enabled"] is True and saved["rollout"] == 50


def test_tc7_flag_change_requires_csrf(fapp, flags_file, as_):
    admin = as_("admin")
    r = admin.c.post("/admin/flags/ai_quiz", data={"enabled": "true"})
    assert r.status_code == 400


@pytest.mark.parametrize("data", [{"rollout": "101"}, {"rollout": "-1"}, {"rollout": "abc"},
                                  {"enabled": "maybe"}, {}])
def test_admin_input_validation(fapp, flags_file, as_, data):
    before = flags_file.read_text(encoding="utf-8")
    assert as_("admin").post("/admin/flags/ai_quiz", data=data).status_code == 400
    assert flags_file.read_text(encoding="utf-8") == before


def test_admin_unknown_flag_is_404(as_):
    assert as_("admin").post("/admin/flags/nope", data={"enabled": "false"}).status_code == 404


# =====================================================================
# 熱重載的防呆、flag debt
# =====================================================================
def test_broken_file_keeps_last_good_config(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100})
    betty = as_("betty")
    assert betty.get("/api/probe/explain").status_code == 200
    flags_file.write_text("{ not json", encoding="utf-8")           # 手滑存了壞掉的檔案
    assert betty.get("/api/probe/explain").status_code == 200       # 不會整個掛掉或亂開關
    assert fapp.extensions["flags"].last_error


def test_missing_file_fails_closed(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "x", "FLAGS_FILE": str(tmp_path / "none.json")})
    with app.app_context():
        assert not is_on("ai_quiz", "betty")


@pytest.mark.parametrize("bad", [
    {"enabled": "yes"}, {"rollout": 150}, {"rollout": 5.5}, {"owner": ""}, {"expires": "next week"},
])
def test_invalid_flag_values_rejected(bad):
    raw = json.loads(REPO_FLAGS.read_text(encoding="utf-8"))
    raw["ai_quiz"].update(bad)
    with pytest.raises(ValueError):
        parse_flags(json.dumps(raw))


def test_repo_flags_json_is_valid_owned_and_not_expired():
    """Flag debt gate：每個 flag 都要有負責人與到期日；過期 CI 就失敗，提醒清掉 flag。"""
    flags = parse_flags(REPO_FLAGS.read_text(encoding="utf-8"))
    assert set(flags) == set(KNOWN_FLAGS)
    for f in flags.values():
        assert f.owner
        assert f.expires >= date.today(), f"{f.name} expired on {f.expires}; owner {f.owner} should remove it"


def test_unknown_flag_name_in_decorator_fails_fast():
    with pytest.raises(ValueError):
        feature_required("ai_quizz")


def test_allow_list_for_internal_testers(fapp, flags_file):
    """Rollout 第一階段：rollout=0，只開給內部名單。"""
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 0, "allow": ["betty"]})
    with fapp.app_context():
        assert is_on("ai_quiz", "betty") and not is_on("ai_quiz", "ben")


# =====================================================================
# 監控與自動 kill switch
# =====================================================================
def test_metrics_split_by_variant(fapp, flags_file):
    from app import metrics
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 0, "allow": ["betty"]})
    users = store(fapp).users
    with fapp.app_context():
        assert metrics.record("explanation_shown", "ai_quiz", user=users["betty"]) == "on"
        assert metrics.record("question_retry", "ai_quiz", user=users["ben"]) == "off"
        assert metrics.record("question_retry", "ai_quiz", user=users["alice"]) == "excluded"
        metrics.record("explanation_latency_ms", "ai_quiz", user=users["betty"], value=1200)
        metrics.record("explanation_latency_ms", "ai_quiz", user=users["betty"], value=3000)
        with pytest.raises(ValueError):
            metrics.record("explanation_shwon", "ai_quiz", user=users["betty"])   # 打錯事件名
    s = summary(fapp)["ai_quiz"]
    assert s["on"]["events"]["explanation_shown"] == 1
    assert s["off"]["events"]["question_retry"] == 1
    assert s["excluded"]["events"]["question_retry"] == 1
    assert s["on"]["latency_ms_p50"] == 2100


def test_auto_kill_when_llm_cost_over_budget(fapp, flags_file):
    from app import metrics
    fapp.config["LLM_DAILY_BUDGET_USD"] = 0.05
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100})
    betty = store(fapp).users["betty"]
    with fapp.app_context():
        for _ in range(5):
            metrics.record("llm_call", "ai_quiz", user=betty, cost_usd=0.01, latency_ms=900)
        assert is_on("ai_quiz", "betty")                     # 剛好 0.05，還沒超過
        metrics.record("llm_call", "ai_quiz", user=betty, cost_usd=0.01)
        assert not is_on("ai_quiz", "betty")                 # 超過預算 → 自動關閉
    saved = json.loads(flags_file.read_text(encoding="utf-8"))["ai_quiz"]
    assert saved["enabled"] is False and saved["updated_by"] == "auto:llm_budget"


def test_auto_kill_when_error_rate_spikes(fapp, flags_file, as_):
    fapp.config.update(KILL_MIN_REQUESTS=10, KILL_ERROR_RATE=0.2, PROPAGATE_EXCEPTIONS=False)
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 100})
    betty = as_("betty")
    for _ in range(8):
        assert betty.get("/api/probe/explain").status_code == 200
    codes = [betty.get("/api/probe/boom").status_code for _ in range(2)]
    assert codes == [500, 500]                               # 10 個請求、2 個錯 = 20% → 觸發
    assert betty.get("/api/probe/explain").status_code == 403
    assert fapp.extensions["flags"].audit[0]["actor"] == "auto:error_rate"
    assert summary(fapp)["ai_quiz"]["on"]["error_rate"] == 0.2


def test_admin_console_shows_flags_and_metrics(fapp, flags_file, as_):
    write_flags(flags_file, ai_quiz={"enabled": True, "rollout": 50})
    admin = as_("admin")
    html = admin.get("/admin/flags").get_data(as_text=True)
    for text in ("ai_quiz", "spaced_reminder", "ON · 50%", "一鍵全關", "Cindy5180", "2026-12-31",
                 "explanation_latency_ms", "reminder_sent", "blocked"):
        assert text in html
    data = admin.get("/admin/flags/metrics.json").get_json()
    assert set(data) == set(FLAGS) and set(data["ai_quiz"]) == {"on", "off", "excluded"}
