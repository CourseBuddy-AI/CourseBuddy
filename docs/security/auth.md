# 認證與授權設計（Developer A：陳儀珊）

## 四個決策

| 決策 | 選擇 | 理由 | 現況 |
| --- | --- | --- | --- |
| 登入方式 | Google OAuth 2.0 Authorization Code + OIDC（Authlib） | 學生都有學校 Google 帳號，不用自己存密碼；Authlib 會驗 state、nonce 與 id_token 簽章 | 程式與測試完成；等 `GOOGLE_CLIENT_ID/SECRET` 放進 Secrets 後實測。開發環境用示範登入，`APP_ENV=production` 時關閉 |
| Session | Flask 簽章 cookie（HttpOnly、SameSite=Lax、正式環境 Secure、8 小時過期） | 不需要額外的 Redis 或資料庫；cookie 只存 `uid`，角色每次請求從伺服器讀，改角色立即生效 | 完成；用 `session_epoch` 補上「登出後舊 cookie 仍有效」的缺點 |
| 角色與屬性 | RBAC：`student`、`beta_tester`、`admin`；ABAC：講義 `owner_id == user.id` 才能讀寫 | 角色決定能用哪些功能（只有 beta_tester 會被分流）；資源擁有者決定能看哪份資料（防 IDOR）。admin 也不能讀學生講義（最小權限） | 完成 |
| 錯誤回應碼 | 未登入 401（頁面導向登入）；角色或擁有者不符 403；flag OFF 打 API 403（組內決定）；資源不存在 404 | 403 讓前端與測試容易判斷「被擋」，和 401 分清楚 | 完成；flag OFF 由賴的 `feature_required` 實作 |

## Session 三種做法比較

| | Flask 簽章 cookie（採用） | JWT | Server-side session |
| --- | --- | --- | --- |
| 狀態存哪 | 瀏覽器 cookie，伺服器用 SECRET_KEY 驗簽 | 瀏覽器（通常放 header 或 cookie） | 伺服器（Redis／DB），cookie 只放 session id |
| 撤銷 | 原生做不到；本專案用 `session_epoch` 補 | 過期前做不到，要另做黑名單 | 刪掉伺服器那筆即可 |
| 額外元件 | 無 | 無 | 需要 Redis／DB |
| 適合 | 單一 Flask 服務、示範規模 | 多服務、行動 App、API 閘道 | 多台機器、需要強制登出 |

## 共用介面

| 名稱 | 用法 |
| --- | --- |
| `g.user` | 目前使用者（`.id`、`.role`），未登入是 `None` |
| `@login_required` | 未登入：`/api/*` 回 401，頁面導向 `/login` |
| `@role_required("admin")` | 角色不符 403 |
| `can_view_material(user, m)` | 是否為擁有者 |
| `get_material_or_abort(mid)` | 路由用：不存在 404、不是自己的 403 |
| `csrf_token()` | 表單放 `<input type="hidden" name="csrf" value="{{ csrf_token() }}">`；API 用 `X-CSRF-Token` header |

組員模組提供 `init_app(app)`，檔名是 `app/flags.py`、`app/features.py`、`app/upload.py` 就會自動掛上。

## 實測

```bash
python run.py                  # 終端機 1
bash scripts/devA_probe.sh     # 終端機 2
```

## 環境變數（新增，非機密）

| 變數 | 用途 |
| --- | --- |
| `ADMIN_EMAILS` | 逗號分隔；這些 email 第一次用 Google 登入時是 admin |
| `BETA_TESTER_EMAILS` | 逗號分隔；第一次登入時是 beta_tester（其他人預設 student） |
