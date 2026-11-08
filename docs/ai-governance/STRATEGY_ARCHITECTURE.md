# Taiwan Signal｜目前策略架構（Champion）

> 與 `SYSTEM_BASELINE.md`（系統現況）、`CHANGE_CONTROL.md`（變更控管）並列。
> 本檔描述**線上正式生效**的策略架構，所有數字對照實際程式碼與已凍結的
> `CHAMPION_SNAPSHOT.json`（版本 `champion-208e598d8ffb`）。

最後更新：2026-11-08

---

## 0. 一句話定位

**正式 Champion 是「規則式（rule-based）多時間框架波段策略」**，入口
`signal_engine.generate_signal_tw`——**不是 AI 模型**。AI/模型（`learning_model`、
`regime`）目前全是 **shadow-only 研究層，不碰任何線上決策**。

## 1. 資料與時間框架（`TIMEFRAMES`）

| 框架 | 週期 | 根數 | 用途 |
|---|---|---|---|
| 週線 | 5y | 200 | 中期多空方向（`weekly_bias`，放空硬門檻）|
| 日線 | 1y | 250 | 主評分與進出場 |
| 小時線 | 90d | 200 | 短期共振確認 |

指標參數（`INDICATOR_PARAMS`）：EMA 10/20/60、半年線(120)、RSI14、MACD 12/26/9、
ADX14（含 +DI/−DI 方向 `adx_bias`）、ATR14、BB 20±2、量能 20MA；持有 3～20 天。

## 2. 訊號產生流程（四步）

```
多時間框架評分  →  分層 Gate 否決  →  風控/部位  →  落盤+推播
(bull/bear score)  (大盤→個股→強度)   (股數/風險)    (provenance)
```

### ① 評分（`check_multi_timeframe_tw`）

日線各因子分開累加 `bull_score` / `bear_score`，再正規化成 0–100 分：

| 因子 | 權重 |
|---|---|
| EMA 排列方向 | +3 |
| 突破/跌破半年線(120) | +2 |
| RSI（健康區 +1、超買反轉/超賣反彈 +2）| +1~2 |
| MACD（偏多/空 +1、金叉/死叉再 +1）| +1~2 |
| 量增（**依當前多空方向加分**，不再無條件挺多）| +2 |
| ADX 強度/方向 | 加成 |

> 2026-09 已修對稱性 bug：空頭方向現在有與多頭對稱的加分（否則 sell 永遠評不到 A 級）。

### ② 分層 Gate（算分與放行分離，上層否決、下層不看）

放行基本門檻（`SIGNAL_THRESHOLDS`）：`min_score 65`、`min_adx 20`、`min_vol_ratio 1.2`、
`min_rr 1.5`、`min_avg_volume 200 張`、`min_market_cap 5 億`、`high_conf 80`。

## 3. 做多 vs 做空（已啟用雙向，`ENABLE_SHORT_SIGNALS=SHORT_EXECUTABLE=true`）

放空走**更嚴格的三層 Gate**：

1. **大盤 Regime**：大盤明確中期多頭 → 禁止逆勢放空；大盤單日情緒分 ≥ `SHORT_MAX_SENTIMENT 65` → 觀望。
2. **個股方向**：週線必須 `bearish`（硬性，`SHORT_REQUIRE_WEEKLY_BEARISH`）＋ ADX 方向 `-DI>+DI`。
3. **訊號強度**：`min_score 70`、`min_adx 22`、`min_vol_ratio 1.3`（皆高於做多）。

## 4. 部位與風控（frozen 常數，改任一項 → CI 紅 → 需使用者核准）

| 參數 | 值 | 意義 |
|---|---|---|
| `MAX_RISK_PER_TRADE` | **2%** | 單筆最大風險（÷每股風險算股數）|
| `MAX_DAILY_RISK` | **6%** | 當日/組合停損風險上限 |
| `MAX_PORTFOLIO_RISK` | **15%** | 組合風險上限 |
| `MAX_SIMULTANEOUS_POSITIONS` | **5** | 同時持倉上限 |
| `MAX_PER_CORRELATION_GROUP` | **2** | 同相關族群上限 |

部位：`風險預算 ÷ 每股風險` 算股數 → 被「資金 ÷ 持倉上限」「剩餘現金」「流動性」壓制
（`calc_position_size` / `fit_to_cash`）。風控獨立 fail-closed，AI 不得繞過。

## 5. 出場與成本

- **分批出場**（`execution.simulate_staged_exit`，Live 與 Backtest 共用、已一致）：依市值
  分類 `SWING_PARAMS` 設 SL(ATR 倍數)＋TP1/TP2/TP3(RR)，多數分類啟用移動停利。
  例：大型股 SL=1.5×ATR、TP 1.5/2.5/4.0 RR；小型股 SL=2.5×ATR、TP 2/4/6 RR。
- **成本**：手續費 0.1425%（低消 20）、賣出證交稅 0.3%、滑價 0.1%、放空借券年化 4%
  （依持有天數計）。

## 6. 熔斷與事件否決（`CIRCUIT_BREAKER` / 事件）

- 大盤單日：跌 −2% 警戒 / −3.5% 停發；VIX 30 高 / 40 極端。
- 外資賣超 −20 億警戒 / −50 億停發；融資 −5% 警示。
- 單日最多 10 筆、訊號 15 天未觸價強制逾期平倉。
- 重大新聞 −20 分；除權息前 2 日封鎖。

## 7. 版本控管與可回溯（2026-11 建立）

- **凍結**：`strategy_version.config_hash` 把上述所有常數算成 `champion-208e598d8ffb`，
  `test_champion_freeze` 鎖住；改任一被追蹤常數 CI 即紅。
- **可回溯**：每筆訊號落盤蓋 `config_hash`+`strategy_version`（TS-GOV-001）。
- **單位治本**：DB 列一律帶明確 `suggested_shares`（股），消除 1000× 誤讀（TS-DATA-001）。

## 8. 研究層（shadow-only，**不影響 Champion**）

`learning_model`（時序 Walk-Forward＋Purged/Embargo CV，誠實 holdout AUC~0.41）、
`regime`（可解釋 baseline）、因子 IC/分位數/單調性、分數校準——全部只做研究/顯示，
**不覆蓋正式訊號、停損、部位或風控**。

---

> 要調整任一策略/風控數字＝改 Champion 行為，須依 `CHANGE_CONTROL.md`：取得使用者核准 →
> 改 `config.py` → `python scripts/freeze_champion.py` 重產快照 → 同一 PR 提交。
