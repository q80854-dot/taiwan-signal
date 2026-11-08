# Taiwan Signal｜協作工作流（AI_WORKFLOW）

> 對應雙 AI 交接文件 §4、§5 與 Phase 1（TS-GOV-002/003）。本檔定義任務狀態、標準流程、
> 分支/PR 規範、風險標籤、衝突規則與決策紀錄。角色見 `AI_ROLES.md`，正式策略保護見
> `CHANGE_CONTROL.md`。

最後更新：2026-11-08

---

## 1. 標準流程（不得跳關）

```text
需求提出
  → 工程端盤點現有程式與資料流
  → 研究端提出假設、數學規格、驗收標準
  → 工程端反向審查工程可行性與風險
  → 使用者核准開發範圍
  → 工程端獨立分支實作與測試
  → 獨立審查 diff／測試／研究定義
  → Backtest / Walk-Forward / OOS
  → Shadow → Paper
  → 雙方審查 → 使用者最終核准
  → 正式升級（若核准）
```

未通過就回到研究或修正階段。**Shadow／Paper 的結果不得以 Production 名義呈現。**

## 2. 任務狀態機

`PROPOSED` → `DISCOVERY` → `RESEARCH_REVIEW` → `APPROVED_FOR_DEV` → `IMPLEMENTING`
→ `TESTING` → `QUANT_VALIDATION` → `SHADOW` → `PAPER` → `APPROVED_FOR_RELEASE` → `PRODUCTION`

- 失敗標 `REJECTED`；回復舊版標 `ROLLED_BACK`。
- **狀態不得跳級**，須保留歷史紀錄（記於 `DECISION_LOG.md`）。

## 3. 分支與 PR

分支命名（本專案現況：單一工程分支 `claude/github-app-*` 對 `main` 發 PR；下列為目標規範）：
- `main`：正式基準，啟用 branch protection。
- `research/<task-id>-<topic>`：研究與實驗。
- `feature/<task-id>-<topic>`：核准後功能實作。
- `hotfix/<task-id>-<topic>`：經核准緊急修正。

規則：
- **不得讓兩個 AI 同時直接改 `main`。** 每項任務有唯一 owner 與 reviewer。
- 每個 PR 必須走 `.github/pull_request_template.md`：任務 ID／核准紀錄、目的與範圍、
  受影響檔案/API/資料表、diff 摘要、測試與 CI 結果、（適用時）回測與資料洩漏檢查、
  安全/效能影響、**rollback 步驟**。
- CI（`.github/workflows/tests.yml`，push/PR 觸發 pytest）必須綠燈才合併。

## 4. 風險標籤（PR 標題或標籤擇一標示）

| 標籤 | 意義 | 門檻 |
|---|---|---|
| `risk:none` | 文件/測試/顯示，**不碰決策** | CI 綠即可 |
| `risk:research` | 新增研究/shadow 程式，隔離於 Champion | CI 綠 + 不寫正式訊號/風控 |
| `risk:champion` | 改到被追蹤策略/風控常數 | **使用者核准** + 重產快照（見 `CHANGE_CONTROL.md`） |
| `risk:infra` | 部署/金鑰/排程/資料庫結構 | **使用者核准** |

判斷不確定時，往**高**一級標示。

## 5. 檔案修改衝突規則

- 同一任務內，Champion 決策路徑（`signal_engine`/`risk_manager`/`config` 被追蹤常數）
  與研究路徑（`shadow`/`learning*`/`regime`/`backtester` 研究用）**分開 commit**，
  好讓審查與回滾精準。
- 若兩路徑無法分離，標 `risk:champion` 從嚴處理。

## 6. 意見衝突處理

不投票、不以 AI 品牌或語氣決定。各自列出假設、資料、推導、不確定性 → 找出差異來源 →
設計**最小可區分實驗**。實驗完成前**維持 Champion、不採納爭議變更**，最後由使用者決定。

## 7. 決策紀錄

每個影響策略/風控/部署的決定，追加一列到 `DECISION_LOG.md`（日期、任務 ID、決定、理由、
核准人、對應 commit）。這是事後歸因與稽核的單一事實來源。
