# CourseBuddy
 
> AI 課程複習助手：上傳講義 PDF → 自動出題 → 依遺忘曲線提醒回來複習錯題
 
DevSecOps 課程期末專案（Assignment 10：Hypothesis-Driven Development + Feature Toggles）。Final Demo：**2026/12/15**。
 
- 目前專案進度 board：[CourseBuddy-AI Projects](https://github.com/orgs/CourseBuddy-AI/projects/1)
- 詳細分工與進度：[docs/期末專案分工.md](docs/期末專案分工.md)
## 專案目標
 
CourseBuddy 讓修 AI 課的學生上傳講義 PDF，自動產生練習題，並依遺忘曲線提醒回來複習錯題。
 
本專案同時是一個 HDD 實驗：把兩個功能放在 feature toggle 後面，只對實驗組開啟，用數據驗證假設是否成立。
 
## 功能
 
### 核心功能（一直開著，不放 toggle）
 
- 登入與角色權限
- 上傳講義 PDF
- 出題、作答，系統記錄對錯
- 錯題本
### 兩個實驗功能（放在 feature toggle 後面）
 
| Flag | ON | OFF |
| --- | --- | --- |
| `ai_quiz` | 答錯後顯示 AI 解析＋「出自講義第 X 頁」，可回報解析錯誤 | 答錯只顯示正確答案 |
| `spaced_reminder` | 答錯的題在第 1、3、7 天提醒回來重做 | 錯題只留在錯題本 |
 
| 假設 | 成功指標 | 防護指標（不能變差） |
| --- | --- | --- |
| H1 `spaced_reminder`：提醒會讓更多學生回來複習 | 7 天內回來重做錯題的使用者比例比 OFF 組高 20% | 關閉提醒比例 ≤ 10%、錯誤率不上升 |
| H2 `ai_quiz`：解析＋頁碼會讓學生更理解錯在哪裡 | 同一題第二次作答正確率比 OFF 組高 15% | 解析回應 ≤ 5 秒、「解析錯誤」回報率不上升、LLM 成本不超出預算 |
 
### Toggle 規則
 
- 只有 `beta_tester` 角色會被分流到實驗組。
- 分流方式：`sha256(flag:user_id) % 100 < rollout`，同一個使用者每次都落在同一組。
- 伺服器端也會檢查：flag 為 OFF 時直接呼叫 API 會回 **403**（隱藏按鈕不算存取控制）。
- `flags.json` 存檔即生效（熱重載），不用重啟；把 `enabled` 改成 `false` 就是 kill switch。
- 已排程的提醒在發送前會再檢查一次 flag。
## 成員與分工
 
| 成員 | GitHub | 角色 | Task | 負責範圍 |
| --- | --- | --- | --- | --- |
| Juinn | [@Juinn226](https://github.com/Juinn226) | PM | 1 | 成員與角色、Project board、時程；作答與兩個實驗功能 |
| 陳儀珊 | [@yishan2004931022-glitch](https://github.com/yishan2004931022-glitch) | Developer A | 2 | App 骨架、登入、session、RBAC/ABAC、伺服器端權限檢查 |
| 林 | [@a7654599](https://github.com/a7654599) | Developer B | 2 | CI、Dependabot、pip-audit、密鑰管理、PDF 上傳與 LLM 安全、架構圖 |
| 張 | [@a0966568571-bot](https://github.com/a0966568571-bot) | Designer | 3 | 兩個假設、ON/OFF 畫面、user flow |
| 賴 | [@Cindy5180](https://github.com/Cindy5180) | Operator / QA | 3 | flags、rollout、kill switch、監控、測試 |
 
## 技術與架構
 
- Python 3.12、Flask 3.1；資料先放在記憶體
- 角色：`student`、`beta_tester`、`admin`（RBAC），講義與作答紀錄只有擁有者能讀寫（ABAC）
- Session：Flask 簽章 cookie；登入目前為示範登入，計畫改用 Google OAuth 2.0 / OIDC
### 程式結構（開發中）
 
| 檔案 | 內容 | 負責 |
| --- | --- | --- |
| `app/__init__.py`、`run.py`、`store.py` | App 骨架、示範帳號與講義 | 陳儀珊 |
| `auth.py` | 登入、session、`g.user`、`login_required`、`role_required`、`can_view_material`、CSRF | 陳儀珊 |
| `flags.json`、`flags.py`、`metrics.py` | `is_on`、`feature_required`、事件紀錄、Admin flag 控制台 | 賴 |
| `features.py` | 作答與對錯紀錄、AI 解析＋頁碼、第 1/3/7 天提醒 | Juinn |
| 上傳路由 | PDF 上傳檢查 | 林 |
| `templates/`、`style.css` | 畫面與兩個功能的 ON/OFF 區塊 | 陳儀珊、張 |
| `tests/` | pytest（權限、上傳、flags、TC1–TC7） | 各負責人 |
 
## 本機執行
 
```bash
git clone https://github.com/CourseBuddy-AI/CourseBuddy.git
cd CourseBuddy
python -m venv .venv
source .venv/bin/activate        # Windows：.venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env             # 填入 SECRET_KEY 等值，.env 不要 commit
python -m pytest -v
```
 
App 骨架合併後可用 `python run.py` 啟動。
 
### 環境變數
 
| 變數 | 用途 | 機密 |
| --- | --- | --- |
| `APP_ENV` | 執行環境（development） | 否 |
| `SECRET_KEY` | Session 簽章金鑰 | 是 |
| `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET` | Google OIDC 登入（計畫中） | Secret 是 |
| `ALLOWED_EMAIL_DOMAIN` | 允許登入的 email 網域 | 否 |
| `LLM_API_KEY` | AI 解析用的 LLM 金鑰；沒有時用示範文字 | 是 |
| `LLM_MODEL` | LLM 模型名稱 | 否 |
 
## 安全設計（DevSecOps）
 
- **密鑰管理**：機密只放 GitHub Environment `ci` 的 Secrets，CI 以 `${{ secrets.XXX }}` 注入成環境變數；非機密設定放 Variables。`.env` 列在 `.gitignore`，repo 只放 `.env.example`。
- **Secret scanning＋Push protection**：已開啟，推送含金鑰的 commit 會被擋下。
- **SCA**：Dependabot 每週檢查 pip 與 GitHub Actions 並自動開 PR；CI 每次 push／PR 用 `pip-audit --strict` 把關，有已知漏洞就失敗。
- **CI 最小權限**：workflow 預設 `permissions: contents: read`。
- **伺服器端授權**：每個受保護路由都檢查登入、角色與資源擁有者（防 IDOR）；未登入 401、無權限 403。
- **PDF 上傳**：檢查副檔名與檔頭 `%PDF-`、大小與頁數上限，檔名去除路徑。
- **LLM**：講義內容只當資料不當指令（防 prompt injection），輸出用固定 JSON 格式驗證，每人每日呼叫有上限。
## 開發流程
 
1. 每件工作對應一張 issue，放在 Project board 上。
2. 從 `main` 開分支，只改自己負責的檔案。
3. 開 PR，請另一位組員審核；CI（`test`、`sca`）全部通過才合併。
4. 共用介面（`g.user`、`login_required`、`role_required`、`can_view_material`、`is_on`、`feature_required`）的名稱不要自行更改。
## 時程
 
| 時間 | 目標 |
| --- | --- |
| 11/22 | CI、Dependabot 合併；App 骨架與登入權限合併 |
| 11/23 | Flag 系統、兩個實驗功能、上傳檢查、ON/OFF 畫面合併；全員截圖 |
| 11/24 | 繳交 A10 個人報告 |
| 11/25–12/8 | Google 登入、LLM 實測、合併 Dependabot PR、補齊指標與測試 |
| 12/9–12/12 | 小規模 rollout，收集 H1、H2 指標 |
| 12/15 | Final Demo |
 
