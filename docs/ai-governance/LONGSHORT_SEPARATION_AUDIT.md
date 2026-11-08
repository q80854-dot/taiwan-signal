# Taiwan Signal｜多空獨立性稽核（LONGSHORT_SEPARATION_AUDIT）

> 治理 TS-SHORT-001 / Phase 3。完成定義：**多空不混合訓練與績效推論**。本檔逐一核對
> 訓練與各績效/研究推論面是否依 `direction`（buy/sell）分開，並標出唯一一處需注意的混合。
> 相關：`learning.py`、`learning_model.py`、`backtester.py`、`FACTOR_REGISTRY.md`。

最後更新：2026-11-08

---

## 1. 核對結果總表

| 面向 | 是否依方向分開 | 位置 / 說明 |
|---|---|---|
| **模型訓練資料** | ✅ 已分開 | `learning_model.build_dataset(direction)` / `_rows(direction)`（#120）：只取 `kind='candidate' AND direction=?`，不混多空、不混 candidate/rejected/control |
| **因子 IC 報告** | ✅ 已分開 | `learning.factor_ic_report` → `_ic_one(buy)` / `_ic_one(sell)`（:472-477） |
| **分數校準** | ✅ 已分開 | `learning.score_calibration` → `_calib_one(buy)` / `_calib_one(sell)`（:361-366） |
| **績效統計（勝率/平均 R）** | ✅ 可分開檢視 | `_dims()` 含「方向」維度（:223），做多 vs 做空的勝率/平均 R 直接並列 |
| **回測方向統計** | ✅ 已分開 | `backtester` `_dir_stats(direction)`：多/空分別回報 |
| **交易成本** | ✅ 方向正確 | `calc_tw_pnl`：做空課稅腳位用 entry、計入借券費（:251-267） |
| **每股風險/部位** | ✅ 方向正確 | `calc_position_size` 以 `|entry−stop|` 計，方向無關且正確 |

→ **訓練與主要研究推論面都已依方向分開，未混用。**

## 2. ✅ 已修正（原為 ⚠️）：headline「候選 vs 對照組」比較方向混淆

**2026-11-08 已修**：`edge` 比較（`learning.build_report`）改為**只取做多候選**對隨機做多對照
（direction-matched），放空不納入此比較（無隨機空單對照），其獨立表現見「方向」維度。
`edge.direction="buy"`、文案明示「做多訊號 vs 隨機做多對照組」。測試 `test_longshort_edge`
驗證極差的放空候選不會污染此結論。以下保留原問題描述供脈絡。

### （原問題）

`learning._learning_stats`（`/api/learning`）的頂層群組比較（:600, :663-670）：

- **候選群組** `cc`＝所有已結算候選（**buy＋sell 混合**）。
- **對照組** `kc`＝「隨機**做多**」（long-only）。

→ 當候選含放空樣本時，「候選平均 R vs 對照組平均 R」這條**頂層結論**是
**方向組成混淆**（mixed-direction 候選 對 long-only 對照）。

**緩解**：報告本身的「方向」維度（§1）已能分別看多/空，使用者不會因此看不到真實的多空表現；
受影響的只有那一條彙總 delta 的解讀。

**採用方案**：對照組依方向配對——edge 比較只取做多候選對隨機做多對照（放空無隨機對照、
不納入此比較）。已於 2026-11-08 實作（見上）。

## 3. 現況限制（誠實揭露）

- 放空樣本數通常遠少於做多（`SHORT_*` 門檻更嚴、`ENABLE_SHORT_SIGNALS` 2026-10 才開），
  故做空的 IC/勝率/平均 R 多半仍 `insufficient`，結論待樣本累積。
- 本稽核確認「**不混合**」這件事成立（§1）；放空**獨立績效的正式報告**（足夠樣本後的
  多空分層 avg R、信賴區間、成本後）待樣本足夠再產出，對應計劃書 Phase 3 交付「多空績效報告」。
