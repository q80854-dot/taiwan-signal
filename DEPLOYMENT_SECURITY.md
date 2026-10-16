# Render 部署安全收尾清單

此清單對應 PR #46（`security/hardening-auth-rate-limits`）。PR 尚未合併；請先等待 GitHub Actions 綠燈，再將變更部署至 staging。

## 上線前環境變數

在 Render 服務的 Environment 設定頁直接建立／更新下列變數。只填入 Render，不要寫進本檔、程式碼、Issue 或 CI log。

| 變數 | 必要性 | 設定要求 |
|---|---|---|
| `ADMIN_AUTH_REQUIRED` | 建議明確設為 `1` | 管理後台強制驗證；程式預設亦為啟用。 |
| `ADMIN_PASSWORD` | 必要 | 至少 8 字元；建議使用密碼管理器產生的高熵隨機值。 |
| `GOOGLE_CLIENT_ID` | 若開放 Google 會員登入則必要 | OAuth 用戶端 ID；Authorized redirect URI 必須與應用程式設定的 callback URL 相同。 |
| `GOOGLE_CLIENT_SECRET` | 若開放 Google 會員登入則必要 | OAuth 用戶端密鑰；不得放入前端或 Git。 |

部署後檢查管理登入狀態端點（`/api/admin/status`）回報已設定，並以站主帳號實際完成登入／登出。Google OAuth 要用正式網域走完整 callback 登入流程。不要把密碼或 OAuth secret 貼到 PR 討論。

## 限流邊界與多實例設定

目前應用程式限流器位於 `protect.py`，其計數狀態仍是每個程序自己的記憶體；重啟會清空，各 worker／實例不會共享額度。程式沒有宣稱 Redis 或平台級限流已啟用。

正式環境若使用多 worker 或多實例，請先在 Render 前方的可信 CDN／WAF／API gateway 設每來源限流，至少覆蓋登入與 `/api/*`，並確認只有可信代理能提供 client IP 標頭。若選用 Redis 共享限流，需另行實作原子滑動視窗、TLS／認證、逾時、故障策略與多 worker 測試；不要只加 `REDIS_URL` 環境變數就視為已防護。

在完成共享限流前，將此服務視為單一程序限流；平台擴容時不得把目前限流數字當成全站總額度。

## 尚未完成的程式硬化

- **CSP inline event handlers：** `dashboard.html` 尚有大量 inline handler，CSP 仍保留 `script-src-attr 'unsafe-inline'`。遷移為外部事件綁定／委派後，才能移除例外；需覆蓋動態產生的控制項並做瀏覽器回歸。
- **完整依賴鎖：** `requirements.txt` 已鎖直接依賴，但未提供完整含 hash 的遞移 lock。須以部署使用的 Python 與 Linux 平台解析，讓 Render 與 CI 由同一 lock 安裝，再合併。
- **容量／攻擊測試：** 不在正式站執行壓測。只在隔離 staging 使用合成資料、硬性流量上限及監控，逐級測量並記錄版本與資源指標。

## 部署後驗證

1. 確認 GitHub Actions pytest 全綠，並檢查 PR diff 沒有 secrets。
2. 將 PR 部署至 staging；查看啟動 log 不應出現設定值或 secret。
3. 驗證未登入私有 API 拒絕、站主登入成功、一般會員無法存取管理功能。
4. 驗證安全標頭與 CSP nonce；檢查瀏覽器 console 沒有因政策變更造成的功能錯誤。
5. 確認平台／代理的限流實際生效；跨實例測試通過後，再記錄其限額、來源 IP 信任設定與故障處理。
6. 完成 staging 驗證後才部署正式環境，並保留回復到前一版的路徑。
