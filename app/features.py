"""作答、錯題本，以及兩個實驗功能的路由（負責：Juinn）。

由 app/__init__.py 的 OPTIONAL_MODULES 自動掛上（init_app）。出題邏輯在 quiz_engine.py。

核心功能（一直開著，不放 toggle）
- 出題：POST /materials/<mid>/quiz（LLM 或規則式題目，每人每天有次數上限）
- 作答：POST /quizzes/<qid>/q/<i>，系統記錄對錯；答錯的題進「錯題本」/wrong

ai_quiz（H2）
- ON ：答錯時顯示 AI 解析＋「出自講義第 X 頁」，可「回報解析錯誤」
- OFF：答錯只顯示正確答案
- API：GET  /api/quizzes/<qid>/q/<i>/explanation、POST …/report → @feature_required("ai_quiz")

spaced_reminder（H1）
- ON ：答錯的題排在第 1、3、7 天複習；到期時收到「你有 N 題該複習了」，點進去重做
- OFF：錯題只留在錯題本，沒有排程、沒有提醒
- 發送：send_due_reminders()（`flask --app run send-reminders`，或 admin 按 /admin/reminders/run）
  每一則提醒在「發送前」都用 still_on() 再檢查一次 flag，OFF 就不送（TC5）

所有讀寫都在伺服器端檢查：講義用 get_material_or_abort（ABAC），題目與作答紀錄只有出題的人能讀寫。
事件名稱依張的清單，記在賴的 metrics.py（依 flag × on/off/excluded 分組）。
"""
from __future__ import annotations

import itertools
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import click
from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, url_for)

from . import metrics
from .auth import get_material_or_abort, login_required, role_required
from .flags import enabled_for, feature_required, still_on, variant_of
from .quiz_engine import REVIEW_OFFSETS, make_quiz, review_schedule

bp = Blueprint("features", __name__)

CONFIG_DEFAULTS = {
    "QUIZ_QUESTIONS": 5,          # 每次出幾題
    "QUIZ_DAILY_LIMIT": 20,       # 每人每天最多出題幾次（控制 LLM 成本、防濫用）
    "LLM_BASE_URL": "",           # 空白 = OpenAI；Gemini / Groq 等 OpenAI 相容 API 填自己的網址
    "LLM_USD_PER_1K_TOKENS": 0.0006,
}
# 密鑰與模型只從環境變數讀（本機 .env，CI 由 GitHub Secrets / Variables 注入）
ENV_KEYS = ("LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL")


# ---------------- 資料（先放記憶體，和 store.py 一樣） ----------------
@dataclass
class Quiz:
    id: int
    material_id: int
    owner_id: str
    questions: list[dict]          # question, options, answer, explanation, page, source
    source: str                    # "llm" / "stub"
    created_on: date


@dataclass
class Attempt:
    user_id: str
    quiz_id: int
    qi: int
    chosen: str
    correct: bool
    on: date
    ai_quiz: str                   # 作答當下 ai_quiz 的分組（on / off / excluded），H2 用
    spaced_reminder: str           # 作答當下 spaced_reminder 的分組，H1 用


@dataclass
class WrongItem:
    """錯題本的一題。due = 第 1、3、7 天；stage = 已經完成幾次複習。"""
    user_id: str
    quiz_id: int
    qi: int
    wrong_on: date
    due: list[date]
    stage: int = 0
    reminded_on: set[date] = field(default_factory=set)

    @property
    def done(self) -> bool:
        return self.stage >= len(self.due)

    def next_due(self) -> date | None:
        return None if self.done else self.due[self.stage]

    def is_due(self, today: date) -> bool:
        return not self.done and today >= self.due[self.stage]


class FeatureStore:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.quizzes: dict[int, Quiz] = {}
        self.attempts: list[Attempt] = []
        self.wrong: dict[tuple[str, int, int], WrongItem] = {}
        self.reminders_off: set[str] = set()       # 自己按了「關閉提醒」的使用者
        self.inbox: dict[str, list[dict]] = {}     # 已送出的站內提醒（之後可以改成 Email）
        self.quota: Counter = Counter()            # (user_id, date) -> 今天出題次數

    def add_quiz(self, material_id, owner_id, questions, source, on) -> Quiz:
        q = Quiz(next(self._ids), material_id, owner_id, questions, source, on)
        self.quizzes[q.id] = q
        return q

    def quizzes_of(self, user_id: str, material_id: int | None = None) -> list[Quiz]:
        return [q for q in self.quizzes.values() if q.owner_id == user_id
                and (material_id is None or q.material_id == material_id)]

    def wrong_of(self, user_id: str) -> list[WrongItem]:
        return [w for w in self.wrong.values() if w.user_id == user_id and not w.done]

    def due_of(self, user_id: str, today: date) -> list[WrongItem]:
        return [w for w in self.wrong_of(user_id) if w.is_due(today)]


def fs() -> FeatureStore:
    return current_app.extensions["features"]


def today() -> date:
    """測試可以用 FEATURES_TODAY 模擬「過了幾天」。"""
    return current_app.config.get("FEATURES_TODAY") or date.today()


def users():
    return current_app.extensions["store"].users


# ---------------- 權限 ----------------
def get_quiz_or_abort(qid: int) -> Quiz:
    """題目不存在 404，不是自己的 403（和講義一樣防 IDOR）。"""
    q = fs().quizzes.get(qid)
    if q is None:
        abort(404)
    if g.user is None or q.owner_id != g.user.id:
        current_app.logger.warning("403 IDOR uid=%s quiz=%s owner=%s",
                                   getattr(g.user, "id", None), qid, q.owner_id)
        abort(403)
    return q


def get_question_or_abort(quiz: Quiz, qi: int) -> dict:
    if not 0 <= qi < len(quiz.questions):
        abort(404)
    return quiz.questions[qi]


def has_answered(user_id: str, quiz_id: int, qi: int) -> bool:
    return any(a.user_id == user_id and a.quiz_id == quiz_id and a.qi == qi for a in fs().attempts)


# ---------------- 出題 ----------------
class QuotaExceeded(Exception):
    pass


def create_quiz(material) -> Quiz:
    cfg = current_app.config
    key = (g.user.id, today())
    if fs().quota[key] >= cfg["QUIZ_DAILY_LIMIT"]:
        raise QuotaExceeded
    budget_left = metrics.get().cost_today("ai_quiz") <= cfg["LLM_DAILY_BUDGET_USD"]
    seed = len(fs().quizzes_of(g.user.id, material.id))   # 同一份講義再出一次，選項順序不同
    questions, source, usage = make_quiz(material.pages, cfg, seed=seed,
                                         n=cfg["QUIZ_QUESTIONS"], budget_left=budget_left)
    fs().quota[key] += 1
    if usage:
        metrics.record("llm_call", "ai_quiz", user=g.user,
                       cost_usd=usage["cost_usd"], latency_ms=usage["latency_ms"])
    quiz = fs().add_quiz(material.id, g.user.id, questions, source, today())
    current_app.logger.info("quiz created uid=%s material=%s quiz=%s source=%s n=%d",
                            g.user.id, material.id, quiz.id, source, len(questions))
    return quiz


# ---------------- 作答 ----------------
def record_answer(quiz: Quiz, qi: int, chosen: str) -> dict:
    """記錄對錯、更新錯題本與複習排程，回傳結果（不含解析：解析要看 ai_quiz）。"""
    q = get_question_or_abort(quiz, qi)
    if chosen not in q["options"]:
        abort(400, "choice must be one of the options")
    user, day = g.user, today()
    correct = chosen == q["answer"]
    retry = has_answered(user.id, quiz.id, qi)
    fs().attempts.append(Attempt(user.id, quiz.id, qi, chosen, correct, day,
                                 variant_of("ai_quiz", user), variant_of("spaced_reminder", user)))
    if retry:
        # H2：同一題第二次作答的正確率（value 1 = 答對、0 = 答錯）
        metrics.record("question_retry", "ai_quiz", user=user, value=1 if correct else 0)

    key = (user.id, quiz.id, qi)
    item = fs().wrong.get(key)
    review_completed = False
    if not correct:
        # 新的錯題，或複習時又答錯：從今天重新排第 1、3、7 天
        fs().wrong[key] = WrongItem(user.id, quiz.id, qi, day, review_schedule(day))
    elif item is not None and item.is_due(day):
        item.stage += 1
        review_completed = True
        metrics.record("review_completed", "spaced_reminder", user=user)
    elif item is not None and not item.done and not enabled_for("spaced_reminder", user):
        # OFF 組沒有排程，在錯題本自己重做答對就算訂正完成
        item.stage = len(item.due)
    return {"correct": correct, "answer": q["answer"], "chosen": chosen,
            "retry": retry, "review_completed": review_completed}


def explanation_for(quiz: Quiz, qi: int) -> dict:
    """ai_quiz 的解析＋出處頁碼。呼叫前要先確認使用者在 ON 組（畫面用 flag_on、API 用 feature_required）。"""
    started = time.monotonic()
    q = get_question_or_abort(quiz, qi)
    data = {"explanation": q.get("explanation") or "", "page": q.get("page"),
            "material_id": quiz.material_id, "answer": q["answer"]}
    metrics.record("explanation_shown", "ai_quiz", user=g.user)
    metrics.record("explanation_latency_ms", "ai_quiz", user=g.user,
                   value=round((time.monotonic() - started) * 1000, 1))
    return data


# ---------------- 提醒 ----------------
def send_due_reminders(day: date | None = None) -> dict:
    """把今天到期的複習題通知給使用者。要在 app context 裡呼叫。

    每個人在「發送前」用 still_on() 再檢查 spaced_reminder（排程後 flag 被關掉就不送，記 suppressed）。
    同一題同一天只提醒一次；使用者自己關掉提醒就不送。
    """
    day = day or today()
    result = {"sent": 0, "suppressed": 0, "opted_out": 0}
    by_user: dict[str, list[WrongItem]] = {}
    for w in fs().wrong.values():
        if w.is_due(day) and day not in w.reminded_on:
            by_user.setdefault(w.user_id, []).append(w)
    for uid, items in by_user.items():
        user = users().get(uid)
        if user is None:
            continue
        if uid in fs().reminders_off:
            result["opted_out"] += 1
            continue
        if not still_on("spaced_reminder", user):
            result["suppressed"] += 1
            continue
        for w in items:
            w.reminded_on.add(day)
        fs().inbox.setdefault(uid, []).append({"on": day.isoformat(), "count": len(items)})
        metrics.record("reminder_sent", "spaced_reminder", user=user)
        result["sent"] += 1
    current_app.logger.info("reminders day=%s %s", day, result)
    return result


def reminder_card() -> dict | None:
    """樣板用：目前使用者的提醒卡片資料；不在 spaced_reminder 實驗組時回 None。"""
    user = g.get("user")
    if user is None or not enabled_for("spaced_reminder", user):
        return None
    due = fs().due_of(user.id, today())
    return {"due": due, "count": len(due), "off": user.id in fs().reminders_off,
            "upcoming": sorted((w for w in fs().wrong_of(user.id) if not w.is_due(today())),
                               key=lambda w: w.next_due())}


# ---------------- H1 / H2 實驗數據 ----------------
def experiment_stats() -> dict:
    """依作答當下的分組算 H1、H2 的成功指標（只算 beta_tester 的 on / off 兩組）。"""
    out = {"h1_spaced_reminder": {}, "h2_ai_quiz": {}}
    attempts = fs().attempts
    for variant in ("on", "off"):
        # H1：有答錯過的人裡，7 天內「改天回來」重做錯題的比例
        wrong_first: dict[str, date] = {}
        came_back: set[str] = set()
        for a in attempts:
            if a.spaced_reminder != variant:
                continue
            if not a.correct and a.user_id not in wrong_first:
                wrong_first[a.user_id] = a.on
        for a in attempts:
            start = wrong_first.get(a.user_id)
            # 「回來」= 答錯之後的另一天（7 天內）重做了以前答錯的題
            if (start and a.spaced_reminder == variant
                    and start < a.on <= start + timedelta(days=7) and _was_wrong_before(a)):
                came_back.add(a.user_id)
        n = len(wrong_first)
        out["h1_spaced_reminder"][variant] = {
            "users_with_wrong": n, "came_back_in_7_days": len(came_back),
            "rate": round(len(came_back) / n, 4) if n else None}

        # H2：第一次答錯的題，第二次作答答對的比例
        firsts: dict[tuple, Attempt] = {}
        seconds: dict[tuple, Attempt] = {}
        for a in attempts:
            k = (a.user_id, a.quiz_id, a.qi)
            if k not in firsts:
                firsts[k] = a
            elif k not in seconds:
                seconds[k] = a
        pairs = [(firsts[k], seconds[k]) for k in seconds
                 if not firsts[k].correct and firsts[k].ai_quiz == variant]
        ok = sum(s.correct for _, s in pairs)
        out["h2_ai_quiz"][variant] = {"retried_after_wrong": len(pairs), "correct_on_retry": ok,
                                      "rate": round(ok / len(pairs), 4) if pairs else None}
    return out


def _was_wrong_before(a: Attempt) -> bool:
    return any(b.user_id == a.user_id and b.quiz_id == a.quiz_id and b.qi == a.qi
               and not b.correct and b.on < a.on for b in fs().attempts)


# =====================================================================
# 頁面路由
# =====================================================================
@bp.post("/materials/<int:mid>/quiz")
@login_required
def new_quiz(mid: int):
    m = get_material_or_abort(mid)
    try:
        quiz = create_quiz(m)
    except QuotaExceeded:
        flash(f"今天的出題次數已達上限（{current_app.config['QUIZ_DAILY_LIMIT']} 次），明天再來")
        return redirect(url_for("main.material", mid=mid))
    if not quiz.questions:
        flash("這份講義抓不到可以出題的內容，換一份試試")
        return redirect(url_for("main.material", mid=mid))
    return redirect(url_for("features.quiz_page", qid=quiz.id))


@bp.get("/quizzes/<int:qid>")
@login_required
def quiz_page(qid: int):
    quiz = get_quiz_or_abort(qid)
    only = request.args.get("q", type=int)
    items = [(i, q) for i, q in enumerate(quiz.questions) if only is None or i == only]
    material = current_app.extensions["store"].materials.get(quiz.material_id)
    return render_template("features/quiz.html", quiz=quiz, items=items, material=material,
                           card=reminder_card())


@bp.post("/quizzes/<int:qid>/q/<int:qi>")
@login_required
def answer_page(qid: int, qi: int):
    quiz = get_quiz_or_abort(qid)
    result = record_answer(quiz, qi, request.form.get("choice", ""))
    show_expl = not result["correct"] and enabled_for("ai_quiz", g.user)
    return render_template("features/result.html", quiz=quiz, qi=qi, q=quiz.questions[qi],
                           result=result, expl=explanation_for(quiz, qi) if show_expl else None,
                           card=reminder_card())


@bp.post("/quizzes/<int:qid>/q/<int:qi>/report")
@feature_required("ai_quiz")
def report_page(qid: int, qi: int):
    quiz = get_quiz_or_abort(qid)
    get_question_or_abort(quiz, qi)
    metrics.record("explanation_reported", "ai_quiz", user=g.user)
    flash("已回報這則解析，謝謝！")
    return redirect(url_for("features.quiz_page", qid=qid, q=qi))


@bp.get("/wrong")
@login_required
def wrong_book():
    items = []
    for w in sorted(fs().wrong_of(g.user.id), key=lambda w: (w.wrong_on, w.quiz_id, w.qi)):
        quiz = fs().quizzes[w.quiz_id]
        items.append((w, quiz, quiz.questions[w.qi]))
    return render_template("features/wrong.html", items=items, card=reminder_card(), today=today())


@bp.get("/review/<int:qid>/<int:qi>")
@feature_required("spaced_reminder")
def review_click(qid: int, qi: int):
    """從提醒點進來重做：記 reminder_clicked，再導到那一題。"""
    get_question_or_abort(get_quiz_or_abort(qid), qi)
    metrics.record("reminder_clicked", "spaced_reminder", user=g.user)
    return redirect(url_for("features.quiz_page", qid=qid, q=qi))


@bp.post("/reminders/off")
@feature_required("spaced_reminder")
def reminders_off():
    fs().reminders_off.add(g.user.id)
    metrics.record("reminder_disabled", "spaced_reminder", user=g.user)
    flash("已關閉複習提醒；錯題仍會留在錯題本")
    return redirect(request.referrer if _same_site(request.referrer) else url_for("features.wrong_book"))


@bp.post("/reminders/on")
@feature_required("spaced_reminder")
def reminders_on():
    fs().reminders_off.discard(g.user.id)
    flash("已重新開啟複習提醒")
    return redirect(url_for("features.wrong_book"))


def _same_site(url: str | None) -> bool:
    return bool(url) and url.startswith(request.host_url)


# =====================================================================
# JSON API（不回傳答案；答案只在作答之後才給）
# =====================================================================
def _public(quiz: Quiz) -> dict:
    return {"id": quiz.id, "material_id": quiz.material_id, "source": quiz.source,
            "questions": [{"index": i, "question": q["question"], "options": q["options"]}
                          for i, q in enumerate(quiz.questions)]}


@bp.post("/api/materials/<int:mid>/quiz")
@login_required
def api_new_quiz(mid: int):
    m = get_material_or_abort(mid)
    try:
        quiz = create_quiz(m)
    except QuotaExceeded:
        return jsonify(error="quota_exceeded", status=429,
                       limit=current_app.config["QUIZ_DAILY_LIMIT"]), 429
    return jsonify(_public(quiz)), 201


@bp.get("/api/quizzes/<int:qid>")
@login_required
def api_quiz(qid: int):
    return jsonify(_public(get_quiz_or_abort(qid)))


@bp.post("/api/quizzes/<int:qid>/q/<int:qi>/answer")
@login_required
def api_answer(qid: int, qi: int):
    quiz = get_quiz_or_abort(qid)
    body = request.get_json(silent=True) or {}
    choice = body.get("choice") if isinstance(body.get("choice"), str) else request.form.get("choice", "")
    return jsonify(record_answer(quiz, qi, choice))


@bp.get("/api/quizzes/<int:qid>/q/<int:qi>/explanation")
@feature_required("ai_quiz")
def api_explanation(qid: int, qi: int):
    quiz = get_quiz_or_abort(qid)
    get_question_or_abort(quiz, qi)
    if not has_answered(g.user.id, qid, qi):
        abort(409, "answer the question first")      # 先作答才看得到解析，不能拿來偷看答案
    return jsonify(explanation_for(quiz, qi))


@bp.post("/api/quizzes/<int:qid>/q/<int:qi>/report")
@feature_required("ai_quiz")
def api_report(qid: int, qi: int):
    quiz = get_quiz_or_abort(qid)
    get_question_or_abort(quiz, qi)
    metrics.record("explanation_reported", "ai_quiz", user=g.user)
    return jsonify(ok=True)


@bp.get("/api/wrong")
@login_required
def api_wrong():
    on = enabled_for("spaced_reminder", g.user)
    return jsonify([{"quiz_id": w.quiz_id, "index": w.qi, "wrong_on": w.wrong_on.isoformat(),
                     **({"next_due": w.next_due().isoformat(), "stage": w.stage} if on else {})}
                    for w in fs().wrong_of(g.user.id)])


@bp.get("/api/reminders")
@feature_required("spaced_reminder")
def api_reminders():
    due = fs().due_of(g.user.id, today())
    return jsonify(count=len(due), off=g.user.id in fs().reminders_off,
                   due=[{"quiz_id": w.quiz_id, "index": w.qi, "due": w.next_due().isoformat()}
                        for w in due],
                   inbox=fs().inbox.get(g.user.id, [])[-10:])


# ---------------- Admin：手動觸發提醒、看實驗數據 ----------------
@bp.post("/admin/reminders/run")
@role_required("admin")
def admin_run_reminders():
    r = send_due_reminders()
    flash(f"提醒：送出 {r['sent']} 人、flag OFF 擋下 {r['suppressed']} 人、自己關閉 {r['opted_out']} 人")
    return redirect(url_for("flags.console"))


@bp.get("/admin/experiment.json")
@role_required("admin")
def admin_experiment():
    return jsonify(experiment_stats())


# ---------------- 掛上 app ----------------
def init_app(app) -> None:
    for k in ENV_KEYS:
        if k not in app.config:            # create_app({...}) 明確給的值（測試用 ""）優先於環境變數
            app.config[k] = os.environ.get(k, "")
    for k, v in CONFIG_DEFAULTS.items():
        app.config.setdefault(k, v)
    app.config.setdefault("LLM_DAILY_BUDGET_USD", 1.00)   # flags.py 也有同一個預設
    app.extensions["features"] = FeatureStore()
    app.register_blueprint(bp)
    app.jinja_env.globals.update(
        quizzes_for=lambda m: fs().quizzes_of(g.user.id, m.id) if g.get("user") else [],
        reminder_card=reminder_card,
        review_offsets=REVIEW_OFFSETS,
    )

    @app.cli.command("send-reminders")
    def send_reminders_cmd():
        """把今天到期的複習提醒送出去（可以用 cron 每天跑一次）。"""
        click.echo(send_due_reminders())
