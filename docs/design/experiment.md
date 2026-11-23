# 功能實驗設計：兩個假設、事件與 ON/OFF 畫面（Designer：張）

這份文件寫兩個 flag 要驗證什麼、要看哪些數字，以及 ON 和 OFF 的畫面差在哪裡。
flag 怎麼分組、rollout 和 kill switch 請看賴的 [`docs/ops/feature-flags.md`](../ops/feature-flags.md)；
改版前後的流程圖在 [`user-flow.md`](user-flow.md)。

## 實驗對象

- 只有 `beta_tester` 會被分組。`student` 永遠看 OFF 畫面，不算進實驗（metrics 記為 `excluded`）。
- 分組用 `sha256("flag:user_id") % 100 < rollout`，同一個人整段實驗都在同一組。兩個 flag 分開分組，所以會有 ON/ON、ON/OFF、OFF/ON、OFF/OFF 四種人。
- 正式實驗是 12/10–12/12，`rollout=50`（賴的 rollout 計畫第 2 階段）。

## H1｜`spaced_reminder`：錯題間隔複習提醒

| 要素 | 內容 |
| --- | --- |
| 改變 | 答錯的題目在第 1、3、7 天出現「你有 N 題該複習了」卡片，點進去直接重做那一題 |
| 使用者 | `beta_tester` 中被分到 ON 組、而且至少答錯過一題的學生 |
| 預期影響 | 學生不用自己記得要回來，所以會有更多人改天回來重做錯題 |
| 成功指標 | **7 天回訪率**：答錯過的人裡，在答錯後第 1–7 天（不含當天）回來重做錯題的比例。ON 組 ≥ OFF 組 × 1.2（相對提升 20%）就算成立 |
| 護欄指標 | ① 關閉提醒的人數 ÷ ON 組人數 ≤ 10%（太多人關掉代表提醒在打擾人）② ON 組錯誤率不比 OFF 組高；到 20% 會自動 kill |

我們相信：對 `beta_tester` 開啟錯題間隔複習提醒（第 1、3、7 天），會讓更多學生回來複習。
當 7 天回訪率比 OFF 組高 20%（相對），就代表我們是對的。

**資料來源**：`/admin/experiment.json` 的 `h1_spaced_reminder.on.rate` 和 `.off.rate`（`features.experiment_stats()`）。

**時間上的限制**：12/10 開始到 12/15 demo 只看得到第 1、3 天的提醒。demo 時先用「3 天回訪率」當先行指標，7 天的結果 12/17 補上。

## H2｜`ai_quiz`：AI 解析＋講義出處頁碼

| 要素 | 內容 |
| --- | --- |
| 改變 | 答錯時除了正確答案，再顯示 AI 解析、「出自講義第 X 頁」連結（點了跳到講義那一頁並標亮），以及「回報解析錯誤」 |
| 使用者 | `beta_tester` 中被分到 ON 組、答錯題目的學生 |
| 預期影響 | 學生知道自己錯在哪、可以回去看原文，所以同一題再做一次比較容易答對 |
| 成功指標 | **第二次答對率**：第一次答錯的題目，第二次作答答對的比例。ON 組 ≥ OFF 組 × 1.15（相對提升 15%）就算成立 |
| 護欄指標 | ① 解析回應時間 p95 ≤ 5 秒 ② 回報解析錯誤的次數 ÷ 顯示解析的次數 ≤ 5% ③ LLM 每日成本 ≤ US$1（超過自動 kill） |

我們相信：答錯後顯示 AI 解析和講義出處頁碼，會讓學生更理解錯在哪裡。
當第二次答對率比 OFF 組高 15%（相對），就代表我們是對的。

**資料來源**：`/admin/experiment.json` 的 `h2_ai_quiz`；護欄看 Flag 控制台（`metrics.summary()`）的 `explanation_latency_ms` p95、`explanation_reported`、`llm_cost_usd_today`。

**回報率為什麼是 5%**：OFF 組看不到解析，沒有東西可以比，所以這裡設絕對上限。12/9 內部 5 人試用時會先記一次實際的回報率，如果遠低於 5% 就改用那個數字當基準。

## 事件清單（交給賴，已經寫進 `metrics.py` 的 `DESIGNER_EVENTS`）

| Flag | 事件 | 在哪一步觸發 | 程式位置 | 用在哪個指標 |
| --- | --- | --- | --- | --- |
| `spaced_reminder` | `reminder_sent` | 每天排程送出提醒（送出前再檢查一次 flag） | `send_due_reminders()` | H1 分母的檢查：ON 組有沒有真的收到 |
| | `reminder_clicked` | 點提醒卡片裡的「重做這題」 | `GET /review/<qid>/<qi>` | H1 漏斗：收到 → 點進來 |
| | `review_completed` | 到期的錯題重做答對 | `record_answer()` | H1 漏斗：點進來 → 完成 |
| | `reminder_disabled` | 按「關閉提醒」 | `POST /reminders/off` | H1 護欄 ① |
| `ai_quiz` | `explanation_shown` | 答錯後解析出現在結果頁 | `explanation_for()` | H2 護欄 ② 的分母 |
| | `explanation_latency_ms` | 同上，記產生解析花的時間 | `explanation_for()` | H2 護欄 ① |
| | `question_retry` | 同一題第二次作答，`value` = 1 答對、0 答錯 | `record_answer()` | H2 成功指標 |
| | `explanation_reported` | 按「回報解析錯誤」 | `POST .../report` | H2 護欄 ② |

另外每個請求都會記 `request`、`error`，用來算兩個 flag 共同的錯誤率護欄。

## ON / OFF 畫面

設計原則只有一條：**兩組之間只有 flag 那一塊不一樣，其他東西完全相同**。
例如「再做一次」按鈕在兩組都放在一樣的位置、一樣的樣式。如果只在 ON 組把它做得比較顯眼，第二次答對率變高就分不出是解析的功勞還是按鈕的功勞。

| 畫面 | OFF | ON |
| --- | --- | --- |
| 作答結果頁（答錯） | 你的答案、正確答案、「這題已加入錯題本」、再做一次／下一題 | OFF 的全部，加上「AI 解析」區塊：解析文字、「出自講義第 X 頁」連結、「回報解析錯誤」 |
| 講義頁（從頁碼連結進來） | — | 連到 `#page-X`，那一頁標亮，學生不用自己找 |
| 首頁（我的講義） | 講義列表、上傳講義 | 最上面多一張複習卡片：「你有 N 題該複習了」、每題是第幾次複習、「關閉提醒」 |
| 錯題本 | 錯題列表＋重做 | 每題多一個「下次複習 日期」標籤，上方同一張複習卡片 |

OFF 就是現在的產品，學生不會覺得少了什麼。ON 的區塊都加了 `data-flag="..."`，賴的 TC1 用它檢查 OFF 時畫面上真的沒有這些區塊。
藏起來只是畫面上的事，伺服器端還有 `feature_required` 擋 API（陳儀珊、賴負責）。
