# Feature flags 運維手冊（Operator / QA：賴）

對應 issue [#6](https://github.com/CourseBuddy-AI/CourseBuddy/issues/6)。程式：`flags.json`、`app/flags.py`、`app/metrics.py`、`app/templates/flags_console.html`；測試：`tests/test_flags.py`。

## 1. 架構

```mermaid
flowchart LR
  OP["Operator / Admin"] -->|改檔存檔| F[(flags.json)]
  OP -->|控制台 /admin/flags| C["Flag 控制台<br/>role_required('admin')"]
  C -->|原子寫入 os.replace| F
  M["metrics.py<br/>錯誤率、LLM 成本"] -->|自動 kill switch| F
  F -->|每個請求 os.stat，變了就重讀| S["FlagStore（每個 worker 一份）"]
  U["使用者請求"] --> FR["feature_required(flag)"]
  FR -->|"g.user.role == beta_tester<br/>sha256(flag:user_id) % 100 < rollout"| S
  FR -->|ON| V["功能（Juinn 的 features.py）"]
  FR -->|OFF| X["403 + 記錄 blocked"]
  J["已排程的提醒"] -->|發送前 still_on()| S
  V --> M
```

| 規則 | 做法 |
| --- | --- |
| 誰會被分流 | 只有 `beta_tester`；`student`、`admin` 永遠 OFF（連 `allow` 名單都不算） |
| 怎麼分 | `sha256("flag:user_id") % 100 < rollout`；同一人永遠同一組；兩個 flag 分組互相獨立；調高 rollout 不會把原本 ON 的人踢出去 |
| 伺服器端阻擋 | `feature_required(flag)`：沒登入 401、不在實驗組 403（藏按鈕不算存取控制） |
| 熱重載 | 每次讀 flag 先 `os.stat(flags.json)`，mtime / 大小 / inode 有變就重讀；不用重啟，多個 worker 讀同一份檔案 |
| 防呆 | 檔案壞掉 → 保留上一份正確設定並記 error；一開始就沒有檔案 → 全部 OFF（fail closed） |
| 稽核 | 控制台與自動 kill switch 寫入 `updated_by`、`updated_at`，控制台顯示變更紀錄（誰、何時、前→後、原因） |

### flags.json 欄位

| 欄位 | 說明 |
| --- | --- |
| `enabled` | `false` = kill switch，所有人 OFF |
| `rollout` | 0–100（%），beta_tester 中被分到 ON 的比例 |
| `allow` | 內部測試名單（user id），不看 rollout 一定 ON（仍需是 beta_tester） |
| `owner` | 負責人：決定 rollout、按 kill switch、到期後移除 flag |
| `expires` | 到期日；過期 CI 會失敗（flag debt gate） |

## 2. 兩種狀態與切換步驟

| Flag | `enabled=false` | `enabled=true` + `rollout=100` |
| --- | --- | --- |
| `ai_quiz` | 答錯只顯示正確答案；`/api/...` 回 403 | beta_tester 答錯看到 AI 解析＋「出自講義第 X 頁」 |
| `spaced_reminder` | 錯題只留在錯題本；排程中的提醒不送 | beta_tester 的錯題在第 1、3、7 天提醒 |

截圖：`docs/ops/screenshots/`（`python scripts/flag_screenshots.py` 重新產生）。

**方式 A：改 flags.json 存檔**
1. 編輯 `flags.json`，把 `ai_quiz.enabled` 改成 `true`、`rollout` 改成 `100`，存檔。
2. 不用重啟。下一個請求時 `FlagStore` 發現檔案修改時間變了，重新讀檔（log：`flags reloaded from flags.json: ai_quiz(enabled=True, rollout=100)`）。
3. 驗證：用 Betty（beta_tester）`GET /api/flags` → `{"ai_quiz": true, ...}`；講義頁出現 AI 解析區塊；Alice（student）打 API 仍是 403。

**方式 B：Admin 控制台**
1. 用 admin 登入，打開 `/admin/flags`。
2. 改「狀態 / Rollout」填原因按「儲存」；要關閉就按「只關這個 flag」或「一鍵全關」。
3. 控制台把新設定原子寫回 `flags.json`（其他 worker 下一個請求也會讀到），變更紀錄記下 `admin:<id>`、時間與原因。
4. 驗證同上；`tests/test_flags.py::test_tc5_*` 自動驗證「下一個請求就生效」（本機量到約 1 ms）。

正式環境 flag 的長期設定請走 PR 改 `flags.json`（有 review、有 git 紀錄）；控制台用在實驗中途調整與緊急關閉。

## 3. Rollout 計畫

| 階段 | 時間 | flags.json | 誰在 ON | 進入下一階段的條件 |
| --- | --- | --- | --- | --- |
| 0. Dark launch | 11/23–12/8 | `enabled=false, rollout=0` | 沒有人 | 程式合併、TC1–TC7 通過、Google 登入與 LLM 實測完成 |
| 1. 內部 5 人 | 12/9 | `enabled=true, rollout=0, allow=[5 位組員]` | 組員（角色設為 beta_tester） | 錯誤率 < 5%、解析 p95 ≤ 5 秒、LLM 成本在預算內、提醒在第 1 天正確送出 |
| 2. 50% 實驗 | 12/10–12/12 | `rollout=50, allow=[]` | 一半 beta_tester（實驗組），另一半是對照組 | 每組至少 10 人；防護指標沒變差 |
| 3. 決策 | 12/13–12/15 | 成功 → `rollout=100`；失敗 → `enabled=false` | — | 依 H1 / H2 成功指標與防護指標 |
| 4. 清除 flag | 12/31 前 | 刪除 flag 與 `if` 分支 | — | 到期日 2026-12-31，過期 CI 失敗 |

- 只看 beta_tester：student 的事件記成 `excluded`，不會混進對照組。
- 雜湊分流讓同一人在整個實驗期間固定在同一組，階段 1→2→3 調高 rollout 時原本 ON 的人不會被關掉。
- 限制：H1 的「7 天內回來重做」在 12/15 Demo 前只觀察得到第 1、3 天，Demo 報告用第 3 天的回訪率當先行指標，7 天結果在 12/17 補上。

## 4. Kill switch

| 觸發條件 | 誰觸發 | 怎麼關 | 多久生效 |
| --- | --- | --- | --- |
| ON 組最近 100 個請求錯誤率 ≥ 20%（至少 20 個） | 自動：`auto:error_rate` | `metrics.py` 把該 flag `enabled=false` 寫回 flags.json | 下一個請求 |
| `ai_quiz` 當日 LLM 成本 > `LLM_DAILY_BUDGET_USD`（預設 1 美元，和林一起訂） | 自動：`auto:llm_budget` | 同上 | 下一個請求 |
| 解析 p95 > 5 秒、「解析錯誤」回報率上升、`reminder_disabled` > 10% | Operator 賴（備援：PM Juinn；LLM 問題找林） | 控制台「只關這個 flag」 | 下一個請求 |
| 安全事件（prompt injection 成功、資料外洩、帳號被盜） | 任何 admin | 控制台「一鍵全關」 | 下一個請求 |

- 第二道防線：林在 LLM 廠商後台設每日用量硬上限，就算程式的 kill switch 失效也不會超支。
- 已排程的提醒在發送前呼叫 `still_on("spaced_reminder", user)`，關掉後不再送出（記為 `suppressed`）。
- 重新開啟：控制台把 `enabled` 改回 `true`，同時清掉舊的錯誤率視窗，避免一開就又被自動關掉。
- 關掉之後：在 issue 記錄觸發原因、影響時間、`updated_by`，修好才重新進入原本的 rollout 階段。

## 5. 監控（每個 flag 的 ON / OFF 分開看）

控制台 `/admin/flags` 與 `GET /admin/flags/metrics.json`（admin）；每筆事件也寫進 log（`metric flag=... variant=... event=...`）。

| 指標 | 來源 | 用途 |
| --- | --- | --- |
| 張的事件：`reminder_sent`、`reminder_clicked`、`review_completed`、`reminder_disabled` | features.py 呼叫 `metrics.record` | H1 成功指標（回來重做比例）與防護指標（關閉提醒 ≤ 10%） |
| 張的事件：`explanation_shown`、`question_retry`、`explanation_reported`、`explanation_latency_ms` | 同上 | H2 成功指標（第二次作答正確率）、防護指標（解析 ≤ 5 秒、回報率） |
| `request` / `error` → 錯誤率 | `feature_required` 自動 | 自動 kill switch |
| 解析延遲 p50 / p95 | `explanation_latency_ms`、`llm_call.latency_ms` | 防護指標 ≤ 5 秒 |
| LLM 呼叫次數與成本 | `metrics.record("llm_call", ..., cost_usd=...)` | 預算 kill switch |
| 被擋的 API 呼叫 `blocked` | `feature_required` 自動 | OFF 組或學生直接打 API 的次數（應該都是 403） |
| `suppressed` | `still_on()` 自動 | 關掉後被攔下的提醒數 |

分組：`on`（beta_tester 實驗組）、`off`（beta_tester 對照組，或 flag 關閉）、`excluded`（student / admin）。目前資料在記憶體，重啟會歸零；之後換資料庫時保留同樣的 `record()` 介面。

## 6. 測試案例（`tests/test_flags.py`）

| TC | 情境 | 測試 | 預期 |
| --- | --- | --- | --- |
| TC1 | flag OFF：畫面看不到功能 | `test_tc1_*` | 沒有 `data-flag` 區塊；`/api/flags` 為 false；核心功能還在 |
| TC2 | flag OFF：直接打 API | `test_tc2_*` | 403（未登入 401），記錄 `blocked` |
| TC3 | ON、rollout=100 | `test_tc3_*` | 所有 beta_tester 看得到、API 200 |
| TC4 | rollout=50 | `test_tc4_*` | 同一 user_id 每次一樣（重啟也一樣）；1000 人中 ai_quiz 518、spaced_reminder 467 人 ON |
| TC5 | ON → OFF | `test_tc5_*` | 改檔或控制台後下一個請求 403，不用重啟；排程提醒發送前檢查，0 封送出 |
| TC6 | student / admin | `test_tc6_*` | rollout=100、甚至在 allow 名單也不會 ON；升級成 beta_tester 下一個請求就加入 |
| TC7 | 非 admin 改 flag | `test_tc7_*` | 控制台、儲存、kill、一鍵全關都 403，flags.json 不變；缺 CSRF 400 |

另有：壞檔保留上一份設定、缺檔 fail closed、欄位驗證、flag 到期檢查、LLM 預算與錯誤率自動 kill switch、監控分組。

## 7. 給組員的接法

```python
# Juinn：features.py
from app.flags import enabled_for, feature_required, still_on
from app import metrics

@bp.post("/api/questions/<int:qid>/explanation")
@feature_required("ai_quiz")              # OFF → 403
def explanation(qid): ...
    metrics.record("explanation_shown", "ai_quiz", user=g.user)
    metrics.record("llm_call", "ai_quiz", user=g.user, cost_usd=cost, latency_ms=ms)

def send_due_reminders():                  # 排程
    for r in due:
        if not still_on("spaced_reminder", r.user):   # 發送前再檢查一次
            continue
        send(r); metrics.record("reminder_sent", "spaced_reminder", user=r.user)
```

```jinja
{# 張：material.html，ON 區塊加 data-flag（TC1 會檢查） #}
{% if flag_on("ai_quiz") %}<section data-flag="ai_quiz">…AI 解析＋第 X 頁…</section>
{% else %}<section>正確答案：…</section>{% endif %}
```
