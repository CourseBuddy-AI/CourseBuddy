"""Feature flags：開關、穩定分流、熱重載、kill switch、Admin 控制台（負責：賴 / Operator・QA）。

由 app/__init__.py 的 OPTIONAL_MODULES 自動掛上（init_app）。

共用介面（名稱不要改，組員的程式會用到）：
- is_on(flag, user_id)        flag 開著，而且這個 user_id 被分到實驗組（ON）→ True
                              只看 flag 與 user_id，不看角色；給分流本身與測試用
- enabled_for(flag, user)     加上角色規則：只有 beta_tester 會被分流，student / admin 一律 OFF
                              畫面、排程、API 都用這個（或 feature_required）
- feature_required(flag)      路由 decorator：沒登入 401（頁面導向登入）；不在實驗組 403
- still_on(flag, user)        已排程的工作（例如提醒）在「發送前」再檢查一次；OFF 時不要送
- 樣板裡用 {% if flag_on("ai_quiz") %} … {% endif %}；ON 區塊請加 data-flag="ai_quiz"（TC1 會檢查）

分流：sha256("flag:user_id") % 100 < rollout。
- 同一個人每次都落在同一組（不用存資料、重啟也一樣）
- flag 名稱放進雜湊，兩個 flag 的分組互相獨立（ai_quiz 的實驗組不會剛好也是 spaced_reminder 的實驗組）
- rollout 從 5 調到 50，原本在 ON 的人仍在 ON（bucket < 5 一定也 < 50），使用者不會忽開忽關

熱重載：每次讀 flag 前先 os.stat(flags.json)，修改時間 / 大小 / inode 變了就重讀，
所以改檔存檔或用控制台，下一個請求就生效，不用重啟，多個 worker 也會讀到同一份。
檔案壞掉（JSON 錯、欄位不對）時保留上一份正確設定並記 error log；一開始就讀不到時全部當 OFF（fail closed）。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .auth import login_required, role_required

# 程式裡用到的 flag 名稱。新增 flag 時：flags.json 加一筆＋這裡加一個名字（decorator 會擋打錯字）
KNOWN_FLAGS = ("ai_quiz", "spaced_reminder")
EXPERIMENT_ROLE = "beta_tester"          # 只有這個角色會被分流到實驗組

DEFAULT_FLAGS_FILE = Path(__file__).resolve().parent.parent / "flags.json"


# ---------------- 資料結構 ----------------
@dataclass(frozen=True)
class Flag:
    name: str
    enabled: bool
    rollout: int                       # 0–100（%）
    owner: str                         # 負責人：決定 rollout、按 kill switch、到期後清掉 flag
    expires: date                      # 到期日：過期代表 flag debt，CI 會失敗提醒清除
    allow: tuple[str, ...] = ()        # 內部測試名單：不看 rollout，一定 ON（仍需是 beta_tester）
    description: str = ""
    extra: dict = field(default_factory=dict, compare=False)   # updated_by、updated_at 等

    @property
    def expired(self) -> bool:
        return date.today() > self.expires

    @property
    def days_left(self) -> int:
        return (self.expires - date.today()).days

    def to_json(self) -> dict:
        d = {"description": self.description, "enabled": self.enabled, "rollout": self.rollout,
             "allow": list(self.allow), "owner": self.owner, "expires": self.expires.isoformat()}
        d.update(self.extra)
        return d


class FlagFileError(ValueError):
    pass


def bucket(flag: str, user_id: str) -> int:
    """0–99。同一個 (flag, user_id) 永遠得到同一個數字。"""
    digest = hashlib.sha256(f"{flag}:{user_id}".encode()).hexdigest()
    return int(digest, 16) % 100


def _parse_rollout(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
        raise FlagFileError(f"rollout must be an integer 0–100, got {v!r}")
    return v


def parse_flag(name: str, raw: dict) -> Flag:
    if not isinstance(raw, dict):
        raise FlagFileError(f"{name}: must be an object")
    known = {"description", "enabled", "rollout", "allow", "owner", "expires"}
    if not isinstance(raw.get("enabled"), bool):
        raise FlagFileError(f"{name}: enabled must be true/false")
    try:
        rollout = _parse_rollout(raw.get("rollout"))
    except FlagFileError as e:
        raise FlagFileError(f"{name}: {e}") from None
    owner = raw.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        raise FlagFileError(f"{name}: owner is required")
    try:
        expires = date.fromisoformat(str(raw.get("expires")))
    except ValueError:
        raise FlagFileError(f"{name}: expires must be YYYY-MM-DD") from None
    allow = raw.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
        raise FlagFileError(f"{name}: allow must be a list of user ids")
    return Flag(name, raw["enabled"], rollout, owner.strip(), expires, tuple(allow),
                str(raw.get("description", "")),
                {k: v for k, v in raw.items() if k not in known})


def parse_flags(text: str) -> dict[str, Flag]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise FlagFileError(f"invalid JSON: {e}") from None
    if not isinstance(data, dict):
        raise FlagFileError("top level must be an object")
    return {name: parse_flag(name, raw) for name, raw in data.items()}


# ---------------- FlagStore：讀檔、熱重載、寫檔 ----------------
class FlagStore:
    def __init__(self, path: str | os.PathLike, logger) -> None:
        self.path = Path(path)
        self.log = logger
        self._lock = threading.RLock()
        self._flags: dict[str, Flag] = {}
        self._stat_key = None
        self.reloads = 0                      # 給測試與控制台看：重讀了幾次
        self.reload_errors = 0
        self.last_error: str | None = None
        self.audit: list[dict] = []           # 誰、什麼時候、改了什麼（最新的在前面）
        self._maybe_reload()

    def _stat(self):
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _maybe_reload(self) -> None:
        key = self._stat()
        if key == self._stat_key:
            return
        with self._lock:
            if key == self._stat_key:
                return
            self._stat_key = key
            if key is None:
                self._fail(f"{self.path} not found")
                return
            try:
                flags = parse_flags(self.path.read_text(encoding="utf-8"))
            except (OSError, FlagFileError) as e:
                self._fail(str(e))
                return
            changed = {n for n in flags if flags[n] != self._flags.get(n)}
            self._flags = flags
            self.reloads += 1
            self.last_error = None
            if changed:
                self.log.warning("flags reloaded from %s: %s", self.path.name,
                                 ", ".join(f"{n}(enabled={flags[n].enabled}, rollout={flags[n].rollout})"
                                           for n in sorted(changed)))
            for f in flags.values():
                if f.expired:
                    self.log.warning("flag %s expired on %s (owner %s): remove it", f.name, f.expires, f.owner)

    def _fail(self, msg: str) -> None:
        # 讀不到或格式錯：保留上一份正確設定；一開始就沒有正確設定 → 全部 OFF
        self.reload_errors += 1
        self.last_error = msg
        self.log.error("flags file rejected, keeping last good config: %s", msg)

    # --- 讀 ---
    def get(self, name: str) -> Flag | None:
        self._maybe_reload()
        return self._flags.get(name)

    def all(self) -> dict[str, Flag]:
        self._maybe_reload()
        return dict(self._flags)

    def is_on(self, name: str, user_id: str | None) -> bool:
        f = self.get(name)
        if f is None or not f.enabled or not user_id:
            return False
        if user_id in f.allow:
            return True
        return bucket(name, str(user_id)) < f.rollout

    # --- 寫（控制台、kill switch） ---
    def update(self, name: str, actor: str, reason: str = "", **changes) -> Flag:
        with self._lock:
            self._maybe_reload()
            old = self._flags.get(name)
            if old is None:
                raise KeyError(name)
            if "rollout" in changes:
                changes["rollout"] = _parse_rollout(changes["rollout"])
            if "enabled" in changes and not isinstance(changes["enabled"], bool):
                raise FlagFileError("enabled must be true/false")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            extra = dict(old.extra, updated_by=actor, updated_at=now)
            new = replace(old, **changes, extra=extra)
            flags = dict(self._flags, **{name: new})
            self._write(flags)
            entry = {"at": now, "actor": actor, "flag": name, "reason": reason,
                     "before": {"enabled": old.enabled, "rollout": old.rollout},
                     "after": {"enabled": new.enabled, "rollout": new.rollout}}
            self.audit.insert(0, entry)
            del self.audit[200:]
            self.log.warning("flag change %s by %s: enabled %s->%s rollout %s->%s reason=%r",
                             name, actor, old.enabled, new.enabled, old.rollout, new.rollout, reason)
            return new

    def kill(self, name: str, actor: str, reason: str) -> Flag:
        return self.update(name, actor, reason or "kill switch", enabled=False)

    def kill_all(self, actor: str, reason: str) -> list[Flag]:
        return [self.kill(n, actor, reason) for n, f in self.all().items() if f.enabled]

    def _write(self, flags: dict[str, Flag]) -> None:
        """先寫暫存檔再 os.replace：其他 worker 不會讀到寫到一半的檔案。"""
        text = json.dumps({n: f.to_json() for n, f in flags.items()}, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".flags-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._flags = flags
        self._stat_key = self._stat()


# ---------------- 共用介面 ----------------
def store() -> FlagStore:
    return current_app.extensions["flags"]


def is_on(flag: str, user_id: str | None) -> bool:
    return store().is_on(flag, user_id)


def enabled_for(flag: str, user) -> bool:
    """角色規則＋分流：只有 beta_tester 會進實驗組。"""
    return user is not None and user.role == EXPERIMENT_ROLE and is_on(flag, user.id)


def variant_of(flag: str, user) -> str:
    """監控用的分組：on（實驗組）／off（對照組：beta_tester 但沒分到）／excluded（不在實驗裡）。"""
    if user is None or user.role != EXPERIMENT_ROLE:
        return "excluded"
    return "on" if is_on(flag, user.id) else "off"


def _check_known(flag: str) -> None:
    if flag not in KNOWN_FLAGS:
        raise ValueError(f"unknown flag {flag!r}; known: {KNOWN_FLAGS}")


def feature_required(flag: str):
    """伺服器端阻擋：flag OFF 或使用者不在實驗組 → 403（藏按鈕不算存取控制）。

    同時記錄監控：被擋的呼叫（blocked）、ON 組的請求數與 5xx 錯誤（算錯誤率、自動 kill switch）。
    """
    _check_known(flag)

    def deco(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            from . import metrics
            user = g.user
            if not enabled_for(flag, user):
                metrics.record("blocked", flag, user=user)
                current_app.logger.info("403 flag=%s uid=%s role=%s path=%s",
                                        flag, user.id, user.role, request.path)
                abort(403)
            try:
                resp = current_app.make_response(view(*args, **kwargs))
            except Exception as e:
                code = getattr(e, "code", 500)
                metrics.record_request(flag, user, error=not isinstance(code, int) or code >= 500)
                raise
            metrics.record_request(flag, user, error=resp.status_code >= 500)
            return resp
        return login_required(guarded)
    return deco


def still_on(flag: str, user) -> bool:
    """排程工作（例如第 1/3/7 天的提醒）在發送「前」呼叫：OFF 就不送，並記一筆 suppressed。"""
    _check_known(flag)
    if enabled_for(flag, user):
        return True
    from . import metrics
    metrics.record("suppressed", flag, user=user)
    return False


# ---------------- 路由：目前使用者的 flag、Admin 控制台 ----------------
bp = Blueprint("flags", __name__)


@bp.get("/api/flags")
@login_required
def api_my_flags():
    """前端用：目前登入者每個 flag 是否 ON（只是 UX，真正的擋在 feature_required）。"""
    return jsonify({n: enabled_for(n, g.user) for n in store().all()})


@bp.get("/admin/flags")
@role_required("admin")
def console():
    from . import metrics
    return render_template("flags_console.html", flags=store().all(), fs=store(),
                           summary=metrics.summary(), m=metrics.get(), today=date.today())


def _form_bool(v: str | None) -> bool:
    if v is None:
        return False
    v = v.strip().lower()
    if v in ("true", "1", "on", "yes"):
        return True
    if v in ("false", "0", "off", "no", ""):
        return False
    abort(400, "enabled must be true/false")


def _done(msg: str):
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, message=msg, flags={n: f.to_json() for n, f in store().all().items()})
    flash(msg)
    return redirect(url_for("flags.console"))


@bp.post("/admin/flags/kill-all")
@role_required("admin")
def kill_all():
    """一鍵全關：所有 flag enabled=false。"""
    killed = store().kill_all(f"admin:{g.user.id}", request.form.get("reason", "kill all"))
    return _done(f"已全部關閉：{', '.join(f.name for f in killed) or '（原本就都是關的）'}")


@bp.post("/admin/flags/<name>")
@role_required("admin")
def update_flag(name: str):
    if store().get(name) is None:
        abort(404)
    changes = {}
    if "enabled" in request.form:
        changes["enabled"] = _form_bool(request.form.get("enabled"))
    if "rollout" in request.form:
        try:
            changes["rollout"] = int(request.form["rollout"])
        except ValueError:
            abort(400, "rollout must be an integer")
    if not changes:
        abort(400, "nothing to change")
    try:
        f = store().update(name, f"admin:{g.user.id}", request.form.get("reason", ""), **changes)
    except FlagFileError as e:
        abort(400, str(e))
    if f.enabled:
        from . import metrics
        metrics.get().reset_guard(name)      # 重新開啟時清掉舊的錯誤率視窗，避免立刻又被自動關掉
    return _done(f"{name}：enabled={f.enabled}，rollout={f.rollout}%")


@bp.post("/admin/flags/<name>/kill")
@role_required("admin")
def kill_flag(name: str):
    if store().get(name) is None:
        abort(404)
    store().kill(name, f"admin:{g.user.id}", request.form.get("reason", "kill switch"))
    return _done(f"{name} 已關閉（kill switch），下一個請求生效")


@bp.get("/admin/flags/metrics.json")
@role_required("admin")
def metrics_json():
    from . import metrics
    return jsonify(metrics.summary())


# ---------------- 掛上 app ----------------
CONFIG_DEFAULTS = {
    "FLAGS_FILE": str(DEFAULT_FLAGS_FILE),
    # 自動 kill switch 的門檻（和林一起訂，見 docs/ops/feature-flags.md）
    "KILL_ERROR_RATE": 0.20,          # ON 組最近的請求錯誤率 ≥ 20%
    "KILL_MIN_REQUESTS": 20,          # 至少 20 個請求才判斷，避免 1/1 = 100% 誤觸
    "KILL_WINDOW": 100,               # 看最近 100 個請求
    "LLM_DAILY_BUDGET_USD": 1.00,     # ai_quiz 每日 LLM 成本上限（美元）
}


def init_app(app) -> None:
    for k, v in CONFIG_DEFAULTS.items():
        app.config.setdefault(k, v)
    app.extensions["flags"] = FlagStore(app.config["FLAGS_FILE"], app.logger)
    from . import metrics
    metrics.init_app(app)
    app.register_blueprint(bp)
    app.jinja_env.globals["flag_on"] = lambda name: enabled_for(name, g.get("user"))
