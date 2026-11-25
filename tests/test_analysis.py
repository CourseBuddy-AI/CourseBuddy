"""H1 / H2 實驗結果判定的測試（負責：Juinn，issue #18）。

前半用假資料直接測 evaluate()（不用 app）：門檻、樣本不足、護欄。
後半用真的 app 走一次：admin 才看得到報告、--days 3 先行指標、CLI。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.analysis import evaluate, format_report
from tests.test_features import (DAY0, answer, answer_of, app, as_, both_on,  # noqa: F401  (fixtures)
                                 flags_file, go_to, make_wrong)

CFG = {"LLM_DAILY_BUDGET_USD": 1.00, "KILL_ERROR_RATE": 0.20}


# ---------------- 假資料 ----------------
def fake_stats(h1_on=(10, 6), h1_off=(10, 4), h2_on=(10, 8), h2_off=(10, 6)):
    """h1_* = (答錯過的人, 回來的人)；h2_* = (答錯後重做的題, 答對的題)。"""
    def h1(n, k):
        return {"users_with_wrong": n, "came_back_in_7_days": k, "rate": round(k / n, 4) if n else None}

    def h2(n, k):
        return {"retried_after_wrong": n, "correct_on_retry": k, "rate": round(k / n, 4) if n else None}

    return {"h1_spaced_reminder": {"on": h1(*h1_on), "off": h1(*h1_off)},
            "h2_ai_quiz": {"on": h2(*h2_on), "off": h2(*h2_off)}}


def fake_summary(disabled=0, err_on=0.0, err_off=None, p95=1200, shown=20, reported=0):
    def variant(err, **extra):
        return {"events": {"explanation_shown": extra.get("shown", 0),
                           "explanation_reported": extra.get("reported", 0)},
                "unique_users": {"reminder_disabled": extra.get("disabled", 0)},
                "error_rate": err, "latency_ms_p95": extra.get("p95")}

    return {"spaced_reminder": {"on": variant(err_on, disabled=disabled), "off": variant(err_off)},
            "ai_quiz": {"on": variant(None, p95=p95, shown=shown, reported=reported), "off": variant(None)}}


def guard(result, key, name):
    return next(g for g in result[key]["guardrails"] if g["name"] == name)


# ---------------- 成功指標 ----------------
def test_both_supported_and_numbers_are_right():
    r = evaluate(fake_stats(), fake_summary(), 0.3, CFG)
    h1, h2 = r["h1_spaced_reminder"], r["h2_ai_quiz"]
    assert h1["verdict"] == "supported" and h1["verdict_zh"] == "假設成立"
    assert h1["success"] == {"on_rate": 0.6, "off_rate": 0.4, "lift": 0.5, "min_lift": 0.2, "passed": True}
    assert h2["success"]["lift"] == pytest.approx(0.3333, abs=1e-4)          # 0.8 / 0.6 - 1
    assert h2["verdict"] == "supported"


@pytest.mark.parametrize("came_back_on, expected", [
    (5, "not_supported"),       # ON 50% = OFF 50%，沒有提升
    (6, "supported"),           # 0.6 ≥ 0.5 × 1.2 剛好等於門檻 → 算過
])
def test_h1_lift_threshold_is_inclusive(came_back_on, expected):
    r = evaluate(fake_stats(h1_on=(10, came_back_on), h1_off=(10, 5)), fake_summary(), 0, CFG)
    assert r["h1_spaced_reminder"]["verdict"] == expected


def test_h2_below_15_percent_lift_is_not_supported():
    r = evaluate(fake_stats(h2_on=(10, 6), h2_off=(10, 6)), fake_summary(), 0, CFG)
    assert r["h2_ai_quiz"]["success"]["lift"] == 0.0
    assert r["h2_ai_quiz"]["verdict"] == "not_supported"


def test_off_rate_zero_has_no_lift_but_on_positive_passes():
    r = evaluate(fake_stats(h1_on=(10, 3), h1_off=(10, 0)), fake_summary(), 0, CFG)
    s = r["h1_spaced_reminder"]["success"]
    assert s["lift"] is None and s["passed"] is True


def test_both_zero_is_not_an_improvement():
    r = evaluate(fake_stats(h1_on=(10, 0), h1_off=(10, 0)), fake_summary(), 0, CFG)
    assert r["h1_spaced_reminder"]["verdict"] == "not_supported"


# ---------------- 樣本不足 ----------------
def test_small_sample_gives_no_verdict_even_if_lift_is_big():
    r = evaluate(fake_stats(h1_on=(4, 4), h1_off=(10, 1)), fake_summary(), 0, CFG)
    assert r["h1_spaced_reminder"]["verdict"] == "insufficient_data"
    assert r["h1_spaced_reminder"]["sample"]["on"] == 4


def test_min_sample_is_configurable():
    r = evaluate(fake_stats(h1_on=(4, 4), h1_off=(4, 1)), fake_summary(), 0,
                 {**CFG, "EXPERIMENT_MIN_SAMPLE": 3})
    assert r["h1_spaced_reminder"]["verdict"] == "supported"


# ---------------- 護欄 ----------------
def test_h1_too_many_people_turn_off_reminders():
    r = evaluate(fake_stats(), fake_summary(disabled=2), 0, CFG)           # 2 / 10 = 20% > 10%
    g = guard(r, "h1_spaced_reminder", "關閉提醒比例")
    assert g["value"] == 0.2 and g["passed"] is False
    assert r["h1_spaced_reminder"]["verdict"] == "guardrail_failed"


def test_h1_error_rate_compares_to_off_or_kill_threshold():
    worse_than_off = evaluate(fake_stats(), fake_summary(err_on=0.05, err_off=0.01), 0, CFG)
    assert guard(worse_than_off, "h1_spaced_reminder", "ON 組錯誤率")["passed"] is False
    no_off_data = evaluate(fake_stats(), fake_summary(err_on=0.05, err_off=None), 0, CFG)
    assert guard(no_off_data, "h1_spaced_reminder", "ON 組錯誤率")["limit"] == 0.20
    assert no_off_data["h1_spaced_reminder"]["verdict"] == "supported"


@pytest.mark.parametrize("summary_kw, cost, name", [
    ({"p95": 5200}, 0, "解析回應 p95（ms）"),
    ({"shown": 20, "reported": 2}, 0, "解析錯誤回報率"),                     # 10% > 5%
    ({}, 1.5, "今日 LLM 成本（USD）"),
])
def test_h2_guardrails(summary_kw, cost, name):
    r = evaluate(fake_stats(), fake_summary(**summary_kw), cost, CFG)
    assert guard(r, "h2_ai_quiz", name)["passed"] is False
    assert r["h2_ai_quiz"]["verdict"] == "guardrail_failed"


def test_guardrails_do_not_matter_when_success_fails():
    r = evaluate(fake_stats(h2_on=(10, 6), h2_off=(10, 6)), fake_summary(p95=9999), 0, CFG)
    assert r["h2_ai_quiz"]["verdict"] == "not_supported"


def test_format_report_is_readable():
    text = format_report(evaluate(fake_stats(), fake_summary(disabled=2), 0, CFG))
    assert "H1 spaced_reminder：7 天回訪率  →  護欄沒過，不採用" in text
    assert "ON 60.0%  vs  OFF 40.0%，相對提升 50.0%" in text
    assert "H2 ai_quiz：第二次答對率  →  假設成立" in text


# ---------------- 接上真的 app ----------------
def test_report_endpoint_admin_only(app, flags_file, as_):
    both_on(flags_file)
    betty = as_("betty")
    qid = make_wrong(app, betty)
    go_to(app, DAY0 + timedelta(days=1))
    answer(betty, qid, 0, answer_of(app, qid, 0))
    r = as_("admin").get("/admin/experiment/report.json").get_json()
    assert r["h1_spaced_reminder"]["success"]["on_rate"] == 1.0
    assert r["h1_spaced_reminder"]["verdict"] == "insufficient_data"          # 只有 1 個人
    assert as_("betty").get("/admin/experiment/report.json").status_code == 403
    assert as_("alice").get("/admin/experiment/report.json").status_code == 403


def test_three_day_leading_indicator(app, flags_file, as_):
    both_on(flags_file)
    betty = as_("betty")
    qid = make_wrong(app, betty)
    go_to(app, DAY0 + timedelta(days=5))                                      # 第 5 天才回來
    answer(betty, qid, 0, answer_of(app, qid, 0))
    admin = as_("admin")
    assert admin.get("/admin/experiment/report.json?days=7").get_json()[
        "h1_spaced_reminder"]["success"]["on_rate"] == 1.0
    three = admin.get("/admin/experiment/report.json?days=3").get_json()["h1_spaced_reminder"]
    assert three["window_days"] == 3 and three["success"]["on_rate"] == 0.0


def test_cli_experiment_report(app):
    out = app.test_cli_runner().invoke(args=["experiment-report", "--days", "3"])
    assert out.exit_code == 0, out.output
    assert "3 天回訪率" in out.output and "樣本不足" in out.output
