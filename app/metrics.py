"""監控：依 flag × 分組（on / off / excluded）記錄事件（負責：賴 / Operator・QA）。

分組：
- on        beta_tester，被分到實驗組
- off       beta_tester，沒被分到（對照組）；或 flag 整個關掉
- excluded  student / admin：不在實驗裡，事件另外放，不會汙染對照組

事件：
- Designer（張）定義的：
    spaced_reminder：reminder_sent、reminder_clicked、review_completed、reminder_disabled
    ai_quiz：explanation_shown、question_retry、explanation_reported、explanation_latency_ms
- 系統自動記錄的：
    request / error   feature_required 包住的 API，ON 組請求數與 5xx（錯誤率）
    blocked           flag OFF 或不在實驗組時被 feature_required 擋下（403）
    suppressed        已排程的提醒在發送前檢查 flag 為 OFF，沒有送出
    llm_call          呼叫 LLM（帶 cost_usd、latency_ms）

用法（Juinn 的 features.py）：
    from app import metrics
    metrics.record("explanation_shown", "ai_quiz", user=g.user)
    metrics.record("explanation_latency_ms", "ai_quiz", user=g.user, value=1830)
    metrics.record("llm_call", "ai_quiz", user=g.user, cost_usd=0.0021, latency_ms=1830)

自動 kill switch（guardrails）：
- ON 組最近 KILL_WINDOW 個請求錯誤率 ≥ KILL_ERROR_RATE（且至少 KILL_MIN_REQUESTS 個）→ 該 flag enabled=false
- ai_quiz 當天 LLM 成本 > LLM_DAILY_BUDGET_USD → ai_quiz enabled=false
觸發者記為 auto:error_rate / auto:llm_budget，寫進 flags.json 的 updated_by 與控制台的變更紀錄。
"""
from __future__ import annotations

import statistics
import threading
from collections import Counter, defaultdict, deque
from datetime import date

from flask import current_app

DESIGNER_EVENTS = {
    "spaced_reminder": ("reminder_sent", "reminder_clicked", "review_completed", "reminder_disabled"),
    "ai_quiz": ("explanation_shown", "question_retry", "explanation_reported", "explanation_latency_ms"),
}
SYSTEM_EVENTS = ("request", "error", "blocked", "suppressed", "llm_call")
VARIANTS = ("on", "off", "excluded")
LATENCY_EVENTS = {"explanation_latency_ms"}


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter = Counter()                       # (flag, variant, event) -> 次數
        self.users: defaultdict = defaultdict(set)             # (flag, variant, event) -> 不重複使用者
        self.latency: defaultdict = defaultdict(lambda: deque(maxlen=1000))   # (flag, variant) -> ms
        self.llm_cost: defaultdict = defaultdict(float)        # (flag, variant, date) -> 美元
        self.window: defaultdict = defaultdict(deque)          # flag -> ON 組最近請求是否出錯

    def add(self, event: str, flag: str, variant: str, user_id: str | None = None,
            value: float | None = None, cost_usd: float | None = None, latency_ms: float | None = None):
        if flag not in DESIGNER_EVENTS:
            raise ValueError(f"unknown flag {flag!r}")
        if event not in DESIGNER_EVENTS[flag] and event not in SYSTEM_EVENTS:
            raise ValueError(f"unknown event {event!r} for flag {flag!r}")
        if variant not in VARIANTS:
            raise ValueError(variant)
        key = (flag, variant, event)
        with self._lock:
            self.counts[key] += 1
            if user_id:
                self.users[key].add(user_id)
            if event in LATENCY_EVENTS and value is not None:
                self.latency[(flag, variant)].append(float(value))
            if event == "llm_call":
                if latency_ms is not None:
                    self.latency[(flag, variant)].append(float(latency_ms))
                self.llm_cost[(flag, variant, date.today())] += float(cost_usd or 0)

    def push_outcome(self, flag: str, error: bool, size: int) -> None:
        with self._lock:
            w = self.window[flag]
            w.append(error)
            while len(w) > size:
                w.popleft()

    def recent_error_rate(self, flag: str) -> tuple[float, int]:
        w = self.window[flag]
        return (sum(w) / len(w) if w else 0.0), len(w)

    def cost_today(self, flag: str) -> float:
        today = date.today()
        return sum(v for (f, _, d), v in self.llm_cost.items() if f == flag and d == today)

    def reset_guard(self, flag: str) -> None:
        with self._lock:
            self.window[flag].clear()

    def summary(self) -> dict:
        out: dict = {}
        today = date.today()
        for flag, events in DESIGNER_EVENTS.items():
            out[flag] = {}
            for v in VARIANTS:
                c = {e: self.counts[(flag, v, e)] for e in (*events, *SYSTEM_EVENTS)}
                lat = list(self.latency[(flag, v)])
                req = c["request"]
                out[flag][v] = {
                    "events": c,
                    "unique_users": {e: len(self.users[(flag, v, e)]) for e in events},
                    "error_rate": round(c["error"] / req, 4) if req else None,
                    "latency_ms_p50": round(statistics.median(lat)) if lat else None,
                    "latency_ms_p95": round(_p95(lat)) if lat else None,
                    "llm_calls": c["llm_call"],
                    "llm_cost_usd_today": round(self.llm_cost[(flag, v, today)], 4),
                    "blocked": c["blocked"],
                }
        return out


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


# ---------------- 模組層級介面 ----------------
def get() -> Metrics:
    return current_app.extensions["metrics"]


def record(event: str, flag: str, user=None, variant: str | None = None, value: float | None = None,
           cost_usd: float | None = None, latency_ms: float | None = None) -> str:
    """記一筆事件，回傳它被算在哪一組。variant 不給時依使用者目前的分組決定。"""
    from .flags import variant_of
    v = variant or variant_of(flag, user)
    uid = getattr(user, "id", None)
    get().add(event, flag, v, uid, value, cost_usd, latency_ms)
    current_app.logger.info("metric flag=%s variant=%s event=%s uid=%s value=%s cost_usd=%s",
                            flag, v, event, uid, value, cost_usd)
    if event == "llm_call":
        _check_budget(flag)
    return v


def record_request(flag: str, user, error: bool) -> None:
    """feature_required 用：ON 組的請求與錯誤，順便檢查錯誤率 guardrail。"""
    record("request", flag, user=user)
    if error:
        record("error", flag, user=user)
    cfg = current_app.config
    m = get()
    m.push_outcome(flag, error, cfg["KILL_WINDOW"])
    rate, n = m.recent_error_rate(flag)
    if n >= cfg["KILL_MIN_REQUESTS"] and rate >= cfg["KILL_ERROR_RATE"]:
        _auto_kill(flag, "auto:error_rate",
                   f"error rate {rate:.0%} over last {n} requests ≥ {cfg['KILL_ERROR_RATE']:.0%}")
        m.reset_guard(flag)


def _check_budget(flag: str) -> None:
    if flag != "ai_quiz":
        return
    budget = current_app.config["LLM_DAILY_BUDGET_USD"]
    spent = get().cost_today(flag)
    if spent > budget:
        _auto_kill(flag, "auto:llm_budget", f"LLM cost today ${spent:.4f} > budget ${budget:.2f}")


def _auto_kill(flag: str, actor: str, reason: str) -> None:
    fs = current_app.extensions["flags"]
    f = fs.get(flag)
    if f is not None and f.enabled:
        fs.kill(flag, actor, reason)
        current_app.logger.error("KILL SWITCH %s triggered by %s: %s", flag, actor, reason)


def summary() -> dict:
    return get().summary()


def init_app(app) -> None:
    app.extensions["metrics"] = Metrics()
