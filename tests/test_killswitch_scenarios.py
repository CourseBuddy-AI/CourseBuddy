"""Kill switch 情境測試（負責：賴 / Operator・QA，A11 issue #22）。

和 test_flags.py 的 TC5 不同：TC5 用 probe 路由驗證 flag 機制本身；
這裡用「真的功能路由」（features.py）演一次事故：實驗組正在使用 → Admin 按 kill switch → 確認三件事
  1. 實驗組下一個請求就看不到功能（畫面沒有 data-flag 區塊、/api/flags 回 false）
  2. 直接打功能 API → 403
  3. 已排程的提醒不會送出（記 suppressed）
另外確認：核心功能（出題、作答、錯題本）不受影響、重新開啟後同一批人回到實驗組、變更有稽核紀錄。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app import create_app
from tests.conftest import As
from tests.test_features import (BETTY_MAT, DAY0, REPO_FLAGS, answer, answer_of, fstore, go_to,
                                 make_wrong, new_quiz, send, set_flags, summary, wrong_choice)

FEATURE_APIS = [  # (method, url 樣板)；{qid} 會換成實際的題組 id
    ("get", "/api/quizzes/{qid}/q/0/explanation"),   # ai_quiz
    ("api_post", "/api/quizzes/{qid}/q/0/report"),   # ai_quiz
    ("get", "/api/reminders"),                       # spaced_reminder
    ("get", "/review/{qid}/0"),                      # spaced_reminder（點提醒連結）
]


# ---------------- 測試環境：兩個 flag 都開 100%，模擬實驗進行中 ----------------
@pytest.fixture
def flags_file(tmp_path):
    p = tmp_path / "flags.json"
    p.write_text(REPO_FLAGS.read_text(encoding="utf-8"), encoding="utf-8")
    set_flags(p, ai_quiz={"enabled": True, "rollout": 100, "allow": []},
              spaced_reminder={"enabled": True, "rollout": 100, "allow": []})
    return p


@pytest.fixture
def app(flags_file):
    return create_app({"TESTING": True, "SECRET_KEY": "test-only", "APP_ENV": "development",
                       "FLAGS_FILE": str(flags_file), "FEATURES_TODAY": DAY0,
                       "LLM_API_KEY": "", "LLM_MODEL": ""})


@pytest.fixture
def as_(app):
    return lambda uid: As(app.test_client(), uid)


@pytest.fixture
def betty_in_experiment(app, as_):
    """Betty（beta_tester）正在用：出了一份題、答錯第 1 題 → 有 AI 解析、有排程中的提醒。"""
    betty = as_("betty")
    qid = make_wrong(app, betty)
    assert betty.get("/api/flags").get_json() == {"ai_quiz": True, "spaced_reminder": True}
    assert betty.get(f"/api/quizzes/{qid}/q/0/explanation").status_code == 200
    return betty, qid


def kill_by_console(as_, flag=None, reason="錯誤率暴增"):
    admin = as_("admin")
    url = "/admin/flags/kill-all" if flag is None else f"/admin/flags/{flag}/kill"
    r = admin.post(url, data={"reason": reason})
    assert r.status_code == 302
    return admin


def kill_by_file(flags_file, *flags):
    set_flags(flags_file, **{f: {"enabled": False} for f in flags})


# =====================================================================
# 情境 1：關掉後，實驗組下一個請求就看不到功能
# =====================================================================
@pytest.mark.parametrize("how", ["console", "file"])
def test_s1_experiment_group_loses_feature_on_next_request(app, flags_file, as_, betty_in_experiment, how):
    betty, qid = betty_in_experiment
    html = betty.get(f"/materials/{BETTY_MAT}").get_data(as_text=True)
    assert 'data-flag="ai_quiz"' in html and 'data-flag="spaced_reminder"' in html

    if how == "console":
        kill_by_console(as_)                                   # 一鍵全關
    else:
        kill_by_file(flags_file, "ai_quiz", "spaced_reminder") # Operator 直接改 flags.json

    # 同一個 Betty、同一個 session、不重啟 → 下一個請求就生效
    assert betty.get("/api/flags").get_json() == {"ai_quiz": False, "spaced_reminder": False}
    for url in (f"/materials/{BETTY_MAT}", "/", "/wrong"):
        assert "data-flag=" not in betty.get(url).get_data(as_text=True), url
    # 再答錯一題：只顯示正確答案，沒有 AI 解析
    html = answer(betty, qid, 1, wrong_choice(app, qid, 1)).get_data(as_text=True)
    assert "AI 解析" not in html


def test_s1_kill_one_flag_keeps_the_other(app, as_, betty_in_experiment):
    betty, _ = betty_in_experiment
    kill_by_console(as_, "ai_quiz", reason="AI 解析內容有誤")
    assert betty.get("/api/flags").get_json() == {"ai_quiz": False, "spaced_reminder": True}
    html = betty.get(f"/materials/{BETTY_MAT}").get_data(as_text=True)
    assert 'data-flag="ai_quiz"' not in html and 'data-flag="spaced_reminder"' in html


# =====================================================================
# 情境 2：直接打 API → 403（藏按鈕不算擋，伺服器端也要擋）
# =====================================================================
@pytest.mark.parametrize("method,url", FEATURE_APIS)
def test_s2_feature_api_returns_403_after_kill(app, as_, betty_in_experiment, method, url):
    betty, qid = betty_in_experiment
    kill_by_console(as_)
    r = getattr(betty, method)(url.format(qid=qid))
    assert r.status_code == 403


def test_s2_blocked_calls_are_counted(app, as_, betty_in_experiment):
    betty, qid = betty_in_experiment
    kill_by_console(as_)
    for method, url in FEATURE_APIS:
        getattr(betty, method)(url.format(qid=qid))
    s = summary(app)
    assert s["ai_quiz"]["off"]["events"]["blocked"] == 2
    assert s["spaced_reminder"]["off"]["events"]["blocked"] == 2


def test_s2_not_logged_in_still_401_not_403(app, as_, betty_in_experiment):
    _, qid = betty_in_experiment
    kill_by_console(as_)
    r = app.test_client().get(f"/api/quizzes/{qid}/q/0/explanation")
    assert r.status_code == 401


# =====================================================================
# 情境 3：已排程的提醒不會送出
# =====================================================================
@pytest.mark.parametrize("how", ["console", "file"])
def test_s3_scheduled_reminder_not_sent_after_kill(app, flags_file, as_, betty_in_experiment, how):
    betty, qid = betty_in_experiment
    item = fstore(app).wrong[("betty", qid, 0)]
    assert item.due[0] == DAY0 + timedelta(days=1)            # 已排好第 1 天的提醒

    if how == "console":
        kill_by_console(as_, "spaced_reminder")
    else:
        kill_by_file(flags_file, "spaced_reminder")

    for offset in (1, 3, 7):                                   # 之後每一個提醒日都不送
        assert send(app, DAY0 + timedelta(days=offset))["sent"] == 0
    assert fstore(app).inbox == {}
    assert summary(app)["spaced_reminder"]["off"]["events"]["suppressed"] == 3


def test_s3_admin_manual_run_also_respects_kill(app, as_, betty_in_experiment):
    """Admin 手動按「發送提醒」也一樣會被擋（不是只有排程）。"""
    kill_by_console(as_, "spaced_reminder")
    go_to(app, DAY0 + timedelta(days=1))
    assert as_("admin").post("/admin/reminders/run").status_code == 302
    assert fstore(app).inbox == {}


# =====================================================================
# 關掉之後：核心功能不受影響、可以恢復、有紀錄
# =====================================================================
def test_core_features_still_work_after_kill(app, as_, betty_in_experiment):
    betty, _ = betty_in_experiment
    kill_by_console(as_)
    qid = new_quiz(betty, BETTY_MAT)["id"]                     # 還能出題
    r = answer(betty, qid, 0, answer_of(app, qid, 0))          # 還能作答
    assert r.status_code == 200
    answer(betty, qid, 1, wrong_choice(app, qid, 1))
    assert len(betty.get("/api/wrong").get_json()) >= 1        # 錯題本還在


def test_reenable_brings_same_users_back(app, flags_file, as_, betty_in_experiment):
    betty, qid = betty_in_experiment
    kill_by_console(as_, "spaced_reminder")
    assert send(app, DAY0 + timedelta(days=1))["sent"] == 0

    r = as_("admin").post("/admin/flags/spaced_reminder",
                          data={"enabled": "true", "rollout": "100", "reason": "修好了，重新開啟"})
    assert r.status_code == 302
    assert betty.get("/api/flags").get_json()["spaced_reminder"] is True
    assert send(app, DAY0 + timedelta(days=1))["sent"] == 1    # 錯題資料沒丟，重開後照常提醒


def test_kill_is_audited(app, flags_file, as_, betty_in_experiment):
    kill_by_console(as_, "ai_quiz", reason="AI 解析內容有誤")
    entry = app.extensions["flags"].audit[0]
    assert entry["flag"] == "ai_quiz" and entry["actor"] == "admin:admin"
    assert entry["reason"] == "AI 解析內容有誤"
    assert entry["before"]["enabled"] is True and entry["after"]["enabled"] is False
    assert '"updated_by": "admin:admin"' in flags_file.read_text(encoding="utf-8")


def test_only_admin_can_kill(app, flags_file, as_, betty_in_experiment):
    before = flags_file.read_text(encoding="utf-8")
    for uid in ("alice", "betty"):
        assert as_(uid).post("/admin/flags/kill-all", data={"reason": "x"}).status_code == 403
    assert flags_file.read_text(encoding="utf-8") == before
