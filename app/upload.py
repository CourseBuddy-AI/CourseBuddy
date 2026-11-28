"""
由 app/__init__.py 的 OPTIONAL_MODULES 自動掛上（init_app）。

上傳的檔案是不可信的輸入，依便宜到昂貴的順序檢查，前面沒過就不做後面的事：
  1. 有沒有檔案
  2. 副檔名是 .pdf
  3. 大小：整個請求先被 MAX_CONTENT_LENGTH 擋（Flask 不會把超大請求讀進記憶體），
     檔案本身再檢查一次 UPLOAD_MAX_BYTES
  4. 檔頭是 %PDF-（副檔名可以亂改，檔頭才看得出是不是 PDF）
  5. pypdf 解析得了、沒加密、頁數不超過 UPLOAD_MAX_PAGES（先數頁數，才逐頁擷取文字）
  6. 擷取得到文字（掃描檔沒有文字，AI 出題用不了）

每人每天最多上傳 UPLOAD_DAILY_LIMIT 次（超過回 429 daily_limit）：
  - 在讀檔「之前」先預扣 1 次（檢查＋預扣在同一個 lock 裡），同時送出的請求不會一起穿過上限
  - 上傳失敗（太大、不是 PDF…）會把預扣的次數退回，所以只有成功存進 store 的才算
  - 「一天」以台灣時間（Asia/Taipei）換日；測試可用 FEATURES_TODAY 模擬換日（與出題上限共用）
  - 換日後第一次預扣時，會把前幾天的紀錄清掉
限制：計數和 store 一樣放在記憶體，只在單一 process 內有效。
  - 重啟後次數歸零（講義本身也會一起消失，所以不會變成「可以多傳」以外的問題）
  - 開多個 worker（gunicorn -w N）時每個 worker 各算各的，上限會變成 N 倍；
    不過 store 也是每個 worker 各一份，多 worker 本來就不能用。之後 store 換成資料庫時，計數一起搬過去

PDF 檔本身不落地，只把每頁文字存進 store；所以沒有存檔路徑可以被 path traversal。
擷取出來的文字之後會交給 LLM，一樣是不可信的資料，Prompt 要把它當資料、不是指令。
"""
from __future__ import annotations

import io
import re
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import (Blueprint, current_app, flash, g, jsonify, redirect, request,
                   url_for)

from .auth import login_required

bp = Blueprint("upload", __name__)

DEFAULTS = {
    "UPLOAD_MAX_BYTES": 10 * 1024 * 1024,  # 單檔 10 MB
    "UPLOAD_MAX_PAGES": 50,                # 一份講義最多 50 頁
    "UPLOAD_MAX_CHARS_PER_PAGE": 5000,     # 每頁文字上限，限制之後送進 LLM 的長度
    "UPLOAD_DAILY_LIMIT": 10,              # 每人每天最多上傳幾次（防濫用、控制儲存與之後的 LLM 成本）
}
TAIPEI = ZoneInfo("Asia/Taipei")
FORM_OVERHEAD = 64 * 1024                  # multipart 邊界與其他欄位（csrf、title）的空間

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"[ \t\r\f\v]+")


class UploadError(Exception):
    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status = status
        self.reason = reason      # 給 log 和 API 用的短代碼
        self.message = message    # 給使用者看的中文訊息


# ---------------- 每日上傳次數 ----------------
def today() -> date:
    """台灣時間的今天。測試用 FEATURES_TODAY 模擬換日（和 features.py 的出題上限用同一個設定）。"""
    return current_app.config.get("FEATURES_TODAY") or datetime.now(TAIPEI).date()


class UploadQuota:
    """每人每天的上傳次數。reserve（檢查＋預扣）與 release（退回）都在 lock 裡，不會被別的請求插隊。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: date | None = None
        self._used: dict[str, int] = {}               # user_id -> 今天已用（含預扣中）的次數

    def _roll(self, day: date) -> None:
        if day != self._day:                          # 換日：前幾天的紀錄直接丟掉
            self._day, self._used = day, {}

    def reserve(self, user_id: str, day: date, limit: int) -> bool:
        with self._lock:
            self._roll(day)
            if self._used.get(user_id, 0) >= limit:
                return False
            self._used[user_id] = self._used.get(user_id, 0) + 1
            return True

    def release(self, user_id: str, day: date) -> None:
        with self._lock:
            if day == self._day and self._used.get(user_id, 0) > 0:
                self._used[user_id] -= 1

    def used(self, user_id: str, day: date) -> int:
        with self._lock:
            return self._used.get(user_id, 0) if day == self._day else 0


def _quota() -> UploadQuota:
    return current_app.extensions["upload_quota"]


def reserve_quota(user_id: str) -> date:
    """預扣 1 次，回傳算在哪一天（失敗退回時要退到同一天，不會因為剛好跨午夜退錯天）。"""
    limit, day = current_app.config["UPLOAD_DAILY_LIMIT"], today()
    if not _quota().reserve(user_id, day, limit):
        raise UploadError(429, "daily_limit", f"今天的上傳次數已達上限（{limit} 次），明天再來")
    return day


# ---------------- 檢查 ----------------
def clean_filename(name: str) -> str:
    """只留最後一段檔名、去掉控制字元。檔名只拿來顯示，不拿來存檔。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return _CONTROL.sub("", name).strip()[:100]


def read_upload(file_storage) -> tuple[str, bytes]:
    if file_storage is None or not file_storage.filename:
        raise UploadError(400, "no_file", "請選擇要上傳的 PDF 檔")

    filename = clean_filename(file_storage.filename)
    if not filename.lower().endswith(".pdf"):
        raise UploadError(415, "bad_extension", "只接受 .pdf 檔")

    limit = current_app.config["UPLOAD_MAX_BYTES"]
    data = file_storage.stream.read(limit + 1)   # 最多讀 limit+1 bytes，夠判斷是否超過
    if len(data) > limit:
        raise UploadError(413, "too_large", f"檔案超過 {limit // (1024 * 1024) or 1} MB 上限")
    if not data:
        raise UploadError(400, "empty", "檔案是空的")
    if not data.startswith(b"%PDF-"):
        raise UploadError(415, "bad_magic", "檔案內容不是 PDF（檔頭不是 %PDF-）")
    return filename, data


def extract_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader  # 延遲匯入：沒用到上傳時不用載入

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise UploadError(400, "encrypted", "PDF 有密碼保護，請先移除密碼再上傳")
        count = len(reader.pages)
    except UploadError:
        raise
    except Exception:  # pypdf 對壞檔會丟各種例外，一律視為無法解析
        raise UploadError(400, "unreadable", "PDF 檔損毀或無法解析")

    max_pages = current_app.config["UPLOAD_MAX_PAGES"]
    if count == 0:
        raise UploadError(400, "no_pages", "PDF 沒有任何頁面")
    if count > max_pages:
        raise UploadError(400, "too_many_pages", f"PDF 有 {count} 頁，超過 {max_pages} 頁上限")

    max_chars = current_app.config["UPLOAD_MAX_CHARS_PER_PAGE"]
    pages = []
    try:
        for page in reader.pages:
            text = _SPACES.sub(" ", page.extract_text() or "").strip()
            pages.append(text[:max_chars])
    except Exception:
        raise UploadError(400, "unreadable", "PDF 檔損毀或無法解析")

    if not any(pages):
        raise UploadError(400, "no_text", "讀不到文字（可能是掃描檔），請上傳有文字層的 PDF")
    return pages


def handle_upload():
    """兩個路由共用：檢查、擷取文字、存進 store，回傳新的 Material。"""
    day = reserve_quota(g.user.id)             # 最便宜的檢查放最前面：超過上限就不讀檔
    try:
        filename, data = read_upload(request.files.get("file"))
        pages = extract_pages(data)
        title = _CONTROL.sub("", request.form.get("title", "")).strip()[:100] or filename[:-4] or "未命名講義"
        m = current_app.extensions["store"].add_material(title, g.user.id, pages, filename)
    except BaseException:
        _quota().release(g.user.id, day)       # 沒存成功：退回預扣的次數
        raise
    current_app.logger.info("upload ok uid=%s material=%s bytes=%d pages=%d",
                            g.user.id, m.id, len(data), len(pages))
    return m


def _log_reject(e: UploadError) -> None:
    current_app.logger.warning("upload rejected uid=%s reason=%s", g.user.id, e.reason)


# ---------------- 路由 ----------------
@bp.post("/materials/upload")
@login_required
def upload_form():
    """首頁的上傳表單：成功跳到講義頁，失敗回首頁顯示原因。"""
    try:
        m = handle_upload()
    except UploadError as e:
        _log_reject(e)
        flash(e.message)
        return redirect(url_for("main.dashboard"))
    flash(f"已上傳「{m.title}」，共 {len(m.pages)} 頁")
    return redirect(url_for("main.material", mid=m.id))


@bp.post("/api/materials")
@login_required
def upload_api():
    try:
        m = handle_upload()
    except UploadError as e:
        _log_reject(e)
        return jsonify(error=e.reason, status=e.status, message=e.message), e.status
    return jsonify(id=m.id, title=m.title, owner_id=m.owner_id, pages=len(m.pages)), 201


def _request_too_large(e):
    """整個請求超過 MAX_CONTENT_LENGTH，Flask 在讀表單時就丟 413，還沒進到路由。"""
    limit = current_app.config["UPLOAD_MAX_BYTES"]
    message = f"檔案超過 {limit // (1024 * 1024) or 1} MB 上限"
    current_app.logger.warning("upload rejected uid=%s reason=too_large(request)",
                               getattr(g.get("user"), "id", None))
    if request.path.startswith("/api/"):
        return jsonify(error="too_large", status=413, message=message), 413
    flash(message)
    return redirect(url_for("main.dashboard"))


def init_app(app) -> None:
    for key, value in DEFAULTS.items():
        app.config.setdefault(key, value)
    # Flask 預設 MAX_CONTENT_LENGTH 是 None（不限制），所以不能用 setdefault
    if app.config.get("MAX_CONTENT_LENGTH") is None:
        app.config["MAX_CONTENT_LENGTH"] = app.config["UPLOAD_MAX_BYTES"] + FORM_OVERHEAD
    app.extensions["upload_quota"] = UploadQuota()
    app.register_blueprint(bp)
    app.register_error_handler(413, _request_too_large)
