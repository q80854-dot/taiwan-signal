# 單位稽核報告：shares / lots / 現金 / 風險（2026-11-06）

對應量化改造規格 **P0 #5**「查清 shares、lots、張數、股數、現金、價格與合約數單位」。
本報告追完整資料流，分出「已正確處理 / 確認 bug（本 PR 已修）/ 待你決策的建議」。

> 結論先講：**單位系統整體一致、且開發者過去有意識處理**（多處註解標註歷史遺留）。
> 沒有系統性的部位/風險計算單位錯誤。但有一個**命名陷阱**衍生出 2 個**顯示層 1000× bug**
> （皆已於本 PR 修正），以及 1 個**建議的治本重構**（需你核准，因牽涉多個讀取端）。

---

## 1. 單位定義（真實語意）

| 名稱 | 單位 | 定義 | 來源 |
|---|---|---|---|
| `shares` / `suggested_shares` | 股 | 零股整數股數 | `signal_engine.calc_position_size` 回 `pos["shares"]` |
| `lots`（張） | 張 = 1000 股 | `shares / SHARES_PER_LOT` | `pos["lots"]`（純顯示換算） |
| `risk_twd` | TWD | `shares × |進場價−停損|` | 同上 |
| `position_value` | TWD | `shares × 進場價` | 同上 |
| `risk_pct` | % | `risk_twd / 帳戶資金 × 100` | 同上 |
| `roundtrip_cost` | TWD | `calc_trade_cost(price, shares, …)` 來回手續費＋稅 | `calc_trade_cost(price, shares, …)` |

`calc_position_size`（`signal_engine.py:28-97`）與 `fit_to_cash`（`:100-124`）單位一致、無誤：
部位以「風險預算 ÷ 每股風險」算股數，再被「資金 ÷ 同時持倉上限」與流動性上限壓制，
全程以 **股** 為基準，`lots` 只是 `shares/1000` 的顯示值。

---

## 2. 命名陷阱（根因）

**DB `signals` 表的欄位名叫 `suggested_lots`，但實際存的是「股數」**（歷史遺留）。
程式多處註解已標註此事：`scanner.py:581`、`app.py:916`、`health.py:386`、`state_store.py:421` 等。

於是同一個名字 `suggested_lots` 有**兩種語意，依來源而定**：

| 來源 | `suggested_lots` 的意義 | 是否另有 `suggested_shares` |
|---|---|---|
| 記憶體訊號 dict（`signal_engine` 產生） | **張數**（`pos["lots"]`） | 有（`pos["shares"]`，股數） |
| DB `signals` 列（`get_recent_signals` / `/api/signals`） | **股數**（原始欄位） | 無 |
| `/api/state` active 訊號（`_db_today_signals`，`app.py:884-886`） | **張數**（已換算 `shares/1000`） | 有（= 原始欄位，股數） |

→ 讀取端只要「知道自己的來源」就正確；多數讀取點都有正確區分。危險在於
**用了 DB 來源卻把 `suggested_lots` 當張數**，會 1000× 放大。

---

## 3. 確認 bug（本 PR 已修）

### 3.1 `explain.py` 訊號解釋「建議張數」1000×（我在 #107 引入）
`/api/explain/signal` 以 `get_recent_signals` 的 DB 列餵入，`suggested_lots`＝股數，
但 `explain.py` 原本寫 `建議 {suggested_lots} 張` → 2500 股顯示成「2500 張」。
**修正**：優先取 `suggested_shares`，否則把 `suggested_lots` 當股數，顯示「X 股（約 Y 張）」。
（`explain.py`，含 `test_explain_shares_unit_not_1000x_inflated` 等測試）

### 3.2 Dashboard 歷史訊號卡「建議股數」1000×（既有）
`dashboard.html` s8 主卡 `const sh = s.suggested_shares || (s.suggested_lots||0)*1000`。
active 訊號有 `suggested_shares`（正確）；但**歷史訊號**（`/api/signals`）無，且其
`suggested_lots`＝股數，`×1000` 後變股數×1000。
**修正**：fallback 改為直接當股數（`s.suggested_lots||0`，不再 ×1000）。
（`dashboard.html:4962`）

> 兩者都是**顯示層**錯誤，不影響實際部位/風險計算（真正下單數字來自 active 訊號卡，正確）。

---

## 4. 待你決策的治本建議（未改）

**建議：在 `get_recent_signals` / `get_closed_trades`（`state_store.py:376,401`）統一做
與 `_db_today_signals` 相同的單位正規化**——回傳時令 `suggested_lots`＝張數、補 `suggested_shares`＝股數。
如此所有讀取端（dashboard 歷史卡、explain、未來新端點）都拿到同一套約定，
不必各自記得「這個來源的 lots 是股還是張」，從根本消除此類 bug。

未在本 PR 一起做的原因：它會改變 `/api/signals` 與 `get_closed_trades` 的回傳語意，
牽涉多個讀取端，需逐一確認無回歸——屬於「大於一處的修改」，依你的指示先提案、待核准。
本 PR 先以最小、零風險的顯示層修正處理兩個確認 bug。

---

## 5. 未發現的問題（佐證）

- 風險參數 `MAX_DAILY_RISK=0.06`、`MAX_PORTFOLIO_RISK=0.15`、`MAX_RISK_PER_TRADE=0.02`
  語意一致（帳戶資金比例），`risk_manager` 讀 DB `suggested_lots` 皆當**股數**使用（正確）。
- 放空借券費已於 `calc_tw_pnl`（`backtester.py:266`）依持有天數計入（估計值）。
- `fit_to_cash`、`calc_position_size`、`calc_trade_cost` 單位自洽。

---

## 6. 回滾

本 PR 僅改 `explain.py` 與 `dashboard.html` 兩處顯示邏輯＋本報告文件，revert 即可。
第 4 節的治本重構尚未實作。
