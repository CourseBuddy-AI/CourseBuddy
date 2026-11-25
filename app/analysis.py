"""H1 / H2 實驗結果判定（負責：Juinn，issue #18）。

features.experiment_stats() 算出 ON / OFF 兩組的比例，metrics.summary() 有護欄要看的數字；
這裡把兩邊對照 docs/design/experiment.md 的門檻，判斷每個假設的結論：

    insufficient_data  樣本不足：任一組人數 / 題數 < EXPERIMENT_MIN_SAMPLE，先不下結論
    guardrail_failed   成功指標達標，但有護欄沒過 → 不採用
    supported          成功指標達標，護欄都過 → 假設成立
    not_supported      成功指標沒達標 → 假設不成立

成功指標（相對提升）：ON 組比例 ≥ OFF 組比例 ×（1 + 門檻），而且 ON 組比例 > 0。
OFF 組比例是 0 時無法算相對提升（lift = None），只要 ON 組 > 0 就算達標。

用法：
    GET /admin/experiment/report.json          （admin）
    flask --app run experiment-report           （終端機印出中文報告）
    flask --app run experiment-report --days 3  （demo 前看 3 天回訪的先行指標）

判定邏輯 evaluate() 是純函式，不需要 app，測試可以直接餵假資料。
"""
from __future__ import annotations

import click
from flask import Blueprint, current_app, jsonify, request

from .auth import role_required

bp = Blueprint("analysis", __name__)

# 門檻都來自 docs/design/experiment.md（張）；要改請先改文件
CONFIG_DEFAULTS = {
    "EXPERIMENT_MIN_SAMPLE": 5,        # 每組至少幾個人（H1）/ 幾題（H2）才下結論
    "H1_MIN_LIFT": 0.20,               # 7 天回訪率：ON ≥ OFF × 1.2
    "H1_MAX_DISABLE_RATE": 0.10,       # 護欄①：關閉提醒人數 ÷ ON 組答錯過的人數 ≤ 10%
    "H2_MIN_LIFT": 0.15,               # 第二次答對率：ON ≥ OFF × 1.15
    "H2_MAX_P95_MS": 5000,             # 護欄①：解析回應 p95 ≤ 5 秒
    "H2_MAX_REPORT_RATE": 0.05,        # 護欄②：回報解析錯誤 ÷ 顯示解析 ≤ 5%
}

VERDICT_ZH = {
    "insufficient_data": "樣本不足",
    "guardrail_failed": "護欄沒過，不採用",
    "supported": "假設成立",
    "not_supported": "假設不成立",
}


# ---------------- 判定（純函式） ----------------
def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def _success(on: float | None, off: float | None, min_lift: float) -> dict:
    lift = round(on / off - 1, 4) if on is not None and off else None
    passed = bool(on) and (off is not None) and on >= off * (1 + min_lift)
    return {"on_rate": on, "off_rate": off, "lift": lift, "min_lift": min_lift, "passed": passed}


def _guard(name: str, value, limit, passed: bool) -> dict:
    return {"name": name, "value": value, "limit": limit, "passed": passed}


def _verdict(enough: bool, success: dict, guardrails: list[dict]) -> str:
    if not enough:
        return "insufficient_data"
    if not success["passed"]:
        return "not_supported"
    if not all(g["passed"] for g in guardrails):
        return "guardrail_failed"
    return "supported"


def evaluate(stats: dict, summary: dict, llm_cost_today: float, cfg: dict, h1_days: int = 7) -> dict:
    """stats = features.experiment_stats()；summary = metrics.summary()；cfg 用到上面的門檻。"""
    c = {**CONFIG_DEFAULTS, **{k: v for k, v in cfg.items() if k in CONFIG_DEFAULTS}}
    n_min = c["EXPERIMENT_MIN_SAMPLE"]

    # ---- H1 spaced_reminder ----
    h1_on, h1_off = stats["h1_spaced_reminder"]["on"], stats["h1_spaced_reminder"]["off"]
    sr_on = summary["spaced_reminder"]["on"]
    on_users = h1_on["users_with_wrong"]
    disabled = sr_on["unique_users"]["reminder_disabled"]
    disable_rate = _ratio(disabled, on_users)
    err_on = sr_on["error_rate"]
    err_off = summary["spaced_reminder"]["off"]["error_rate"]
    h1_guards = [
        _guard("關閉提醒比例", disable_rate, c["H1_MAX_DISABLE_RATE"],
               disable_rate is None or disable_rate <= c["H1_MAX_DISABLE_RATE"]),
        # OFF 組不經過 feature_required，通常沒有錯誤率；沒有時改跟 kill switch 門檻比
        _guard("ON 組錯誤率", err_on, err_off if err_off is not None else cfg.get("KILL_ERROR_RATE", 0.20),
               err_on is None or err_on <= (err_off if err_off is not None else cfg.get("KILL_ERROR_RATE", 0.20))),
    ]
    h1_success = _success(h1_on["rate"], h1_off["rate"], c["H1_MIN_LIFT"])
    h1_enough = min(on_users, h1_off["users_with_wrong"]) >= n_min

    # ---- H2 ai_quiz ----
    h2_on, h2_off = stats["h2_ai_quiz"]["on"], stats["h2_ai_quiz"]["off"]
    aq_on = summary["ai_quiz"]["on"]
    p95 = aq_on["latency_ms_p95"]
    report_rate = _ratio(aq_on["events"]["explanation_reported"], aq_on["events"]["explanation_shown"])
    budget = cfg.get("LLM_DAILY_BUDGET_USD", 1.00)
    h2_guards = [
        _guard("解析回應 p95（ms）", p95, c["H2_MAX_P95_MS"], p95 is None or p95 <= c["H2_MAX_P95_MS"]),
        _guard("解析錯誤回報率", report_rate, c["H2_MAX_REPORT_RATE"],
               report_rate is None or report_rate <= c["H2_MAX_REPORT_RATE"]),
        _guard("今日 LLM 成本（USD）", round(llm_cost_today, 4), budget, llm_cost_today <= budget),
    ]
    h2_success = _success(h2_on["rate"], h2_off["rate"], c["H2_MIN_LIFT"])
    h2_enough = min(h2_on["retried_after_wrong"], h2_off["retried_after_wrong"]) >= n_min

    out = {}
    for key, success, guards, enough, sample in (
        ("h1_spaced_reminder", h1_success, h1_guards, h1_enough,
         {"on": on_users, "off": h1_off["users_with_wrong"], "unit": "答錯過的人"}),
        ("h2_ai_quiz", h2_success, h2_guards, h2_enough,
         {"on": h2_on["retried_after_wrong"], "off": h2_off["retried_after_wrong"], "unit": "答錯後重做的題"}),
    ):
        v = _verdict(enough, success, guards)
        out[key] = {"verdict": v, "verdict_zh": VERDICT_ZH[v], "sample": sample, "min_sample": n_min,
                    "success": success, "guardrails": guards}
    out["h1_spaced_reminder"]["window_days"] = h1_days
    return out


# ---------------- 接上 app ----------------
def report(h1_days: int = 7) -> dict:
    from . import features, metrics
    return evaluate(features.experiment_stats(h1_window_days=h1_days), metrics.summary(),
                    metrics.get().cost_today("ai_quiz"), current_app.config, h1_days=h1_days)


def _pct(x) -> str:
    return "—" if x is None else f"{x:.1%}"


def format_report(r: dict) -> str:
    lines = []
    for key, title in (("h1_spaced_reminder", f"H1 spaced_reminder：{r['h1_spaced_reminder']['window_days']} 天回訪率"),
                       ("h2_ai_quiz", "H2 ai_quiz：第二次答對率")):
        h = r[key]
        s = h["success"]
        lines.append(f"{title}  →  {h['verdict_zh']}")
        lines.append(f"  樣本（{h['sample']['unit']}）：ON {h['sample']['on']}、OFF {h['sample']['off']}"
                     f"（每組至少 {h['min_sample']}）")
        lines.append(f"  ON {_pct(s['on_rate'])}  vs  OFF {_pct(s['off_rate'])}，相對提升 {_pct(s['lift'])}"
                     f"（門檻 {_pct(s['min_lift'])}）{'✓' if s['passed'] else '✗'}")
        for g in h["guardrails"]:
            lines.append(f"  護欄 {g['name']}：{g['value'] if g['value'] is not None else '—'}"
                         f"（上限 {g['limit']}）{'✓' if g['passed'] else '✗'}")
        lines.append("")
    return "\n".join(lines).rstrip()


@bp.get("/admin/experiment/report.json")
@role_required("admin")
def admin_report():
    days = request.args.get("days", 7, type=int)
    if days not in (3, 7):
        days = 7
    return jsonify(report(days))


def init_app(app) -> None:
    for k, v in CONFIG_DEFAULTS.items():
        app.config.setdefault(k, v)
    app.register_blueprint(bp)

    @app.cli.command("experiment-report")
    @click.option("--days", type=click.Choice(["3", "7"]), default="7", help="H1 回訪要在幾天內才算")
    def experiment_report_cmd(days):
        """印出 H1 / H2 目前的判定結果。"""
        click.echo(format_report(report(int(days))))
