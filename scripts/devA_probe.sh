#!/usr/bin/env bash
# Developer A：用 curl 繞過畫面直接打後端，實測伺服器端權限檢查。
# 用法：先在另一個終端機執行 python run.py，再執行 bash scripts/devA_probe.sh
# Windows 請用 Git Bash。每一段輸出都可以截圖放進報告。
set -u
B=${BASE_URL:-http://localhost:5000}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

token() {  # 從頁面的 hidden 欄位取出 CSRF token
  curl -s -b "$1" -c "$1" "$B$2" | grep -o 'name="csrf" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"$//'
}
login() {  # 用示範帳號登入，cookie 存在 $TMP/$1
  local jar="$TMP/$1" t
  t=$(token "$jar" /login)
  curl -s -o /dev/null -b "$jar" -c "$jar" -X POST "$B/login" -d "uid=$1" -d "csrf=$t"
}
code() {  # 印出「HTTP 狀態碼  說明」
  local label=$1; shift
  printf '  %s  %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$@")" "$label"
}
me() {  # 印出 /api/me 的內容（壓成一行）
  printf '  /api/me → %s\n' "$(curl -s -b "$1" "$B/api/me" | tr -d ' \n')"
}

echo "== 1. 未登入直接打 API（期望 401）"
code "GET /api/me（沒帶 cookie）" "$B/api/me"
code "GET /api/materials/1（沒帶 cookie）" "$B/api/materials/1"

echo; echo "== 2. IDOR：alice 改網址上的 id 看 bob 的講義（期望 403）"
login alice
code "alice GET /api/materials/1（自己的）" -b "$TMP/alice" "$B/api/materials/1"
code "alice GET /api/materials/2（bob 的）" -b "$TMP/alice" "$B/api/materials/2"
code "alice GET /materials/2（bob 的，頁面）" -b "$TMP/alice" "$B/materials/2"

echo; echo "== 3. RBAC：學生自己把角色改成 admin（期望 403）"
T=$(token "$TMP/alice" /)
code "alice POST /admin/users/alice/role=admin" -b "$TMP/alice" -X POST "$B/admin/users/alice/role" -d role=admin -d "csrf=$T"
me "$TMP/alice"

echo; echo "== 4. CSRF：admin 的 cookie 但沒帶 token（期望 400）"
login admin
code "admin POST 改角色，沒帶 csrf" -b "$TMP/admin" -X POST "$B/admin/users/alice/role" -d role=beta_tester
T=$(token "$TMP/admin" /admin/users)
code "admin POST 改角色，帶正確 csrf" -b "$TMP/admin" -X POST "$B/admin/users/alice/role" -d role=beta_tester -d "csrf=$T"
echo "  alice 不用重新登入，角色已經更新："; me "$TMP/alice"

echo; echo "== 5. 登出後，先前複製的 cookie 也失效（期望 401）"
cp "$TMP/alice" "$TMP/stolen"
T=$(token "$TMP/alice" /)
curl -s -o /dev/null -b "$TMP/alice" -c "$TMP/alice" -X POST "$B/logout" -d "csrf=$T"
code "攻擊者拿登出前複製的 cookie GET /api/me" -b "$TMP/stolen" "$B/api/me"

echo; echo "== 6. 非 admin 改 flag（賴的 flag 控制台合併後才有，期望 403）"
login betty
T=$(token "$TMP/betty" /)
code "betty(beta_tester) POST /admin/flags/ai_quiz" -b "$TMP/betty" -X POST "$B/admin/flags/ai_quiz" -d enabled=false -d "csrf=$T"
