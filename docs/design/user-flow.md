# 改版前後的 user flow（Designer：張）

兩個假設和事件定義在 [`experiment.md`](experiment.md)。
這份文件用流程圖畫出學生從登入到回來複習的每一步，標出兩個 flag 在哪裡分岔、每個事件在哪一步記錄。

## 改版前（兩個 flag 都 OFF）

```mermaid
flowchart LR
  A[登入] --> B[上傳講義 PDF] --> C[用講義出題] --> D[作答]
  D -->|答對| E[下一題]
  D -->|答錯| F[看正確答案<br/>題目進錯題本]
  F --> G{自己記得<br/>要回來嗎？}
  G -->|記得| H[打開錯題本重做]
  G -->|忘了| X[沒有再複習]
  F -.->|想知道為什麼錯| Y[自己翻講義找<br/>不知道在第幾頁]
```

問題有兩個：

1. 答錯之後只看到答案，不知道錯在哪，要自己翻整份講義。
2. 要不要回來複習全靠自己記得，大部分人就停在「忘了」。

## 改版後（兩個 flag 都 ON）

```mermaid
flowchart LR
  A[登入] --> B[上傳講義 PDF] --> C[用講義出題] --> D[作答]
  D -->|答對| E[下一題]
  D -->|答錯| F[看正確答案<br/>題目進錯題本]

  F --> Q{ai_quiz<br/>ON？}
  Q -->|OFF| F2[只有正確答案]
  Q -->|ON| P["AI 解析＋出自講義第 X 頁<br/>📊 explanation_shown<br/>📊 explanation_latency_ms"]
  P --> P1[點頁碼<br/>跳到講義那一頁並標亮]
  P --> P2["回報解析錯誤<br/>📊 explanation_reported"]
  P1 --> R[再做一次]
  P --> R
  F2 --> R
  R --> R2["第二次作答<br/>📊 question_retry（答對 1／答錯 0）"]

  F --> S{spaced_reminder<br/>ON？}
  S -->|OFF| S0[錯題只在錯題本<br/>靠自己回來]
  S -->|ON| T["第 1、3、7 天排程送提醒<br/>📊 reminder_sent"]
  T --> U["首頁出現「你有 N 題該複習了」<br/>點「重做這題」<br/>📊 reminder_clicked"]
  U --> V["重做答對<br/>📊 review_completed"]
  V -->|還沒到第 7 天| T
  U --> W["關閉提醒<br/>📊 reminder_disabled"]
```

- 菱形是 flag 的分岔點。兩個 flag 分開分組，所以同一個學生可能 `ai_quiz` ON、`spaced_reminder` OFF。
- 📊 是事件。每個事件都會帶上「這個人當下在哪一組」（on / off / excluded），所以同一個事件可以直接拿 ON 組和 OFF 組來比。
- OFF 那條路就是改版前的產品，沒有拿掉任何東西。

## 每個事件對應到哪個指標

```mermaid
flowchart TB
  subgraph H1["H1 spaced_reminder"]
    e1[reminder_sent] --> e2[reminder_clicked] --> e3[review_completed]
    e4[reminder_disabled]
    m1(["成功：7 天回訪率 ON ≥ OFF × 1.2"])
    g1(["護欄：關閉提醒 ≤ 10%、錯誤率不上升"])
    e3 --> m1
    e4 --> g1
  end
  subgraph H2["H2 ai_quiz"]
    f1[explanation_shown] --> f2[question_retry]
    f3[explanation_reported]
    f4[explanation_latency_ms]
    m2(["成功：第二次答對率 ON ≥ OFF × 1.15"])
    g2(["護欄：p95 ≤ 5 秒、回報率 ≤ 5%、LLM ≤ US$1／天"])
    f2 --> m2
    f3 --> g2
    f4 --> g2
  end
```

兩個成功指標是用作答紀錄（每次作答都記下當下的分組）直接算的，見 `features.experiment_stats()`；事件是拿來看護欄和漏斗。

`reminder_sent → reminder_clicked → review_completed` 是一個漏斗。如果 H1 沒有成立，可以從這裡看是卡在哪一段：提醒沒送出去、送了沒人點，還是點了沒做完。
