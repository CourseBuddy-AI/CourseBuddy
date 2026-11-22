"""產生兩個 flag 的 ON / OFF 截圖與切換證據（負責：賴 / Operator・QA）。

    pip install playwright && python -m playwright install chromium
    python scripts/flag_screenshots.py            # 輸出到 docs/ops/screenshots/

- 用 flags.json 的暫存複本，不會改到 repo 的檔案
- 每個 flag 拍兩種狀態：enabled=false、enabled=true + rollout=100
  每種狀態拍：Admin 控制台、beta_tester（Betty）看到的講義頁、Betty 直接打 API 的結果
- 最後示範用控制台按 kill switch：下一個請求就 403、變更紀錄寫下誰觸發
張的 ON/OFF 區塊與 Juinn 的 API 合併後重跑一次，講義頁就會出現真正的 ON/OFF 畫面。
"""
from __future__ import annotations

import html
import json
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Blueprint  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from app.flags import feature_required  # noqa: E402

OUT = ROOT / "docs" / "ops" / "screenshots"
PORT = 5057
BASE = f"http://127.0.0.1:{PORT}"
FLAGS = ("ai_quiz", "spaced_reminder")
STATES = {"off": {"enabled": False, "rollout": 0}, "on100": {"enabled": True, "rollout": 100}}


def demo_api() -> Blueprint:
    """和正式功能相同的 feature_required 保護；Juinn 的 API 合併後可改拍真正的路由。"""
    bp = Blueprint("demo_flags", __name__)

    @bp.get("/api/demo/ai_quiz/explanation")
    @feature_required("ai_quiz")
    def explain():
        return {"explanation": "過擬合是模型把訓練資料的雜訊也記住…", "source_page": 3}

    @bp.get("/api/demo/spaced_reminder/schedule")
    @feature_required("spaced_reminder")
    def schedule():
        return {"remind_on_days": [1, 3, 7]}
    return bp


def write_flag(path: Path, flag: str, **changes) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data[flag].update(changes)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


EVIDENCE = """<!doctype html><meta charset=utf-8><style>
body{{display:inline-block;font-family:"Noto Sans CJK TC","Noto Sans TC",sans-serif;margin:0;padding:24px;background:#0f172a;color:#e2e8f0;width:760px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#94a3b8;font-size:13px;margin-bottom:16px}}
.row{{font-family:"DejaVu Sans Mono",monospace;font-size:14px;padding:10px 12px;border-radius:8px;background:#1e293b;margin-bottom:10px;white-space:pre-wrap;word-break:break-all}}
.ok{{color:#4ade80}} .no{{color:#f87171}} .k{{color:#93c5fd}}
</style><h1>{title}</h1><div class=sub>{sub}</div>{rows}"""


def evidence(page, title: str, sub: str, calls: list[tuple[str, object, str]]) -> None:
    """calls：(誰, 這個人的 page（用他的 cookie）, url)。把每個請求的狀態碼與回應排成一張圖。"""
    rows = []
    for who, as_page, url in calls:
        r = as_page.request.get(BASE + url)
        cls = "ok" if r.status < 400 else "no"
        try:
            body = json.dumps(json.loads(r.text()), ensure_ascii=False)
        except ValueError:
            body = r.text().strip()
        rows.append(f'<div class=row><span class=k>{who} → GET {url}</span>\n'
                    f'<span class={cls}>HTTP {r.status}</span>  {html.escape(body)}</div>')
    page.set_content(EVIDENCE.format(title=title, sub=sub, rows="".join(rows)))


def login(ctx, name: str):
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.get_by_role("button", name=name).click()
    page.wait_for_url(BASE + "/")
    return page


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    flags_file = tmp / "flags.json"
    shutil.copy(ROOT / "flags.json", flags_file)
    for f in FLAGS:
        write_flag(flags_file, f, **STATES["off"])

    app = create_app({"SECRET_KEY": "screenshots", "APP_ENV": "development", "FLAGS_FILE": str(flags_file)})
    app.register_blueprint(demo_api())
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which("google-chrome") or None)
        opts = dict(viewport={"width": 1280, "height": 900}, device_scale_factor=2, locale="zh-TW")
        admin = login(browser.new_context(**opts), "Admin")
        betty = login(browser.new_context(**opts), "Betty")
        alice = login(browser.new_context(**opts), "Alice")

        def snap(page, name, **kw):
            path = OUT / f"{name}.png"
            (page.locator(kw.pop("selector")).screenshot(path=path) if "selector" in kw
             else page.screenshot(path=path, full_page=kw.get("full", False)))
            shots.append(path.name)

        for flag in FLAGS:
            api = {"ai_quiz": "/api/demo/ai_quiz/explanation",
                   "spaced_reminder": "/api/demo/spaced_reminder/schedule"}[flag]
            for state, values in STATES.items():
                write_flag(flags_file, flag, **values)          # 切換方式 1：改 flags.json 存檔
                admin.goto(BASE + "/admin/flags")
                snap(admin, f"{flag}_{state}_console", selector=f"#flag-{flag}")
                betty.goto(BASE + "/materials/3")
                snap(betty, f"{flag}_{state}_betty_material", full=True)
                ev = betty.context.new_page()
                label = "enabled=false" if state == "off" else "enabled=true, rollout=100"
                evidence(ev, f"{flag}：{label}",
                         f"改 flags.json 存檔後的下一個請求（未重啟） · {datetime.now():%Y-%m-%d %H:%M:%S}",
                         [("Betty (beta_tester)", betty, "/api/flags"),
                          ("Betty (beta_tester)", betty, api),
                          ("Alice (student)", alice, api)])
                snap(ev, f"{flag}_{state}_api", selector="body")
                ev.close()
            write_flag(flags_file, flag, **STATES["off"])

        # 切換方式 2：控制台 kill switch（全部打開 → 一鍵全關）
        for f in FLAGS:
            write_flag(flags_file, f, **STATES["on100"])
        admin.goto(BASE + "/admin/flags")
        snap(admin, "killswitch_1_before", full=True)
        admin.fill("form.fc-killall input[name=reason]", "演練：錯誤率暴增")
        admin.click("#kill-all")
        admin.wait_for_load_state()
        snap(admin, "killswitch_2_after", full=True)
        ev = betty.context.new_page()
        evidence(ev, "Kill switch 後 Betty 的下一個請求",
                 f"Admin 在控制台按「一鍵全關」之後（未重啟） · {datetime.now():%Y-%m-%d %H:%M:%S}",
                 [("Betty (beta_tester)", betty, "/api/flags"),
                  ("Betty (beta_tester)", betty, "/api/demo/ai_quiz/explanation"),
                  ("Betty (beta_tester)", betty, "/api/demo/spaced_reminder/schedule")])
        snap(ev, "killswitch_3_betty_next_request", selector="body")
        ev.close()

        # 非 admin 打控制台
        alice.goto(BASE + "/admin/flags")
        snap(alice, "tc7_student_admin_flags_403")
        browser.close()
    server.shutdown()
    print("\n".join(shots))


if __name__ == "__main__":
    main()
