# Taiwan Signal｜Regime baseline 現況與增量價值方法（REGIME_STATUS）

> 治理 TS-REGIME-001 / Phase 4。完成定義：**不接正式策略，先 OOS 比較**。本檔記錄 Regime
> baseline 的設計、與決策的隔離、增量價值如何量測，以及為何目前只建地基不做策略路由。
> 相關：`regime.py`、`shadow.py`、`learning.py`、`FACTOR_REGISTRY.md`、`STRATEGY_ARCHITECTURE.md`。

最後更新：2026-11-08

---

## 1. 現況：可解釋規則 baseline（已建、shadow-only）

`regime.py`（`regime_snapshot()`，端點 `/api/regime`，#119）用已抓到的大盤資料組出三維市場狀態：

| 維度 | 來源 / 規則 | 狀態值 |
|---|---|---|
| **趨勢 trend** | 大盤中期趨勢（`fetch_market_regime` 的 trend） | bullish / bearish / neutral / unknown |
| **波動 volatility** | 加權指數 20 日年化波動（`_realized_vol_annual`）規則分級 | 低 / 中 / 高（heuristic，可日後校準） |
| **流動性 liquidity** | 近 2 月 TWSE 量能比（`_liquidity_state`） | 擴張 / 收斂 / 持平 |

**隔離界線（硬性）**：模組 docstring 與回傳 `note` 都明載「**僅供研究與顯示，未接入線上訊號決策**」。
放空閘門與訊號產生維持現狀（`fetch_market_regime`），**不依 regime baseline 路由或切換策略**。

## 2. 增量價值怎麼量（已有基礎設施）

「regime 有沒有增量價值」＝它能不能預測訊號成敗/前向報酬。這已被現有機制涵蓋：

- `shadow.snapshot()` 已把 **`regime`（趨勢）與 `sentiment`（大盤情緒分）** 記進每筆候選的特徵快照
  （`shadow.py:377-379`）。
- 因此 `learning.factor_ic_report()` 會像對待其他 ~50 個因子一樣，對 `regime`/`sentiment`
  算 **IC / 五分位淨 R / 單調性 / BH-FDR**（見 `FACTOR_REGISTRY.md §5`）。
- 另外 `_dims()` 的分組統計也能把已結算樣本依 regime/情緒分桶，看各桶的勝率與平均 R。

→ **不需要另建一套 OOS 管線就能先觀察 regime 的關聯性**；差別在「觀察關聯」vs「正式策略路由」。

## 3. 為何只建地基、不做策略路由（誠實揭露）

- **樣本不足**：`ENABLE_SHORT_SIGNALS`/regime baseline 都是 2026-10~11 才上線，已結算樣本的
  regime 分桶多半仍 `insufficient`，無法穩健估計各 regime 下的策略優劣。
- **過度擬合風險**：用 ~20 個交易日的資料決定「哪個 regime 用哪套策略」，極易擬合雜訊。
- 故依治理原則**先建可解釋 baseline、持續以 shadow 累積樣本**，待樣本足夠再做：
  嚴格時間序列 OOS、機率校準、漂移與特徵穩定性檢查，同樣本同成本比較基準。

## 4. 進入「策略路由（Regime→訊號）」的門檻（未來、需核准）

要讓 regime **實際影響決策**（例如某 regime 下收緊/放寬門檻、切換多空權重），須先：
1. 足夠的分 regime 已結算樣本（每桶達 `_IC_MIN_N`/`MIN_DAYS`）。
2. OOS（Purged+Embargo 時序 CV）證明分 regime 策略**顯著優於**不分 regime 的 Champion。
3. 成本後、分層（產業/流動性）穩健，無資料洩漏。
4. 走 `CHANGE_CONTROL.md` 升級門檻 + **使用者核准**（這會是 risk:champion）。

在此之前，regime 維持 shadow-only、不接決策。
