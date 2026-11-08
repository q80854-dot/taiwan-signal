# Taiwan Signal｜因子登錄表（FACTOR_REGISTRY）

> 治理 TS-FACTOR-001 / Phase 3。把 `shadow.snapshot()` 每筆候選所記錄的可量測特徵（factor）
> 逐一登錄：定義、類別、做多方向假設、資料來源。這些因子目前**只記錄、不影響訊號分數**，
> 由 `learning.factor_ic_report()` 以已結算 shadow 樣本算 IC / 分位數 / 單調性 / BH-FDR 驗證。
> 相關：`DATA_DICTIONARY.md`、`SYSTEM_BASELINE.md §2.3`、`learning.py`、`shadow.py`。

最後更新：2026-11-08　特徵 schema：`fs-v1-2026-11-08`（`shadow.FEATURE_SCHEMA_VERSION`）

---

## 0. 怎麼讀這張表

- **方向假設**：對「做多」而言，因子值越大越看多（＋）或越看空（−）的先驗；空單對稱解讀。
  這只是假設，**是否真有預測力以 IC 實證為準**（正負號代表方向、|IC| 越大關聯越強）。
- **驗證機制**（`learning.factor_ic_report`，`_ic_one`）：對已結算樣本（label=`r_multiple`）算
  Spearman IC、常態近似 p 值、五分位淨 R 報酬與單調性（遞增/遞減/非單調）、並用
  Benjamini-Hochberg（q=0.10）控制多因子偽發現率，前後兩段時間方向一致才標「較可靠」。
- **樣本門檻**：已結算樣本未達 `_IC_MIN_N` 時不計 IC（樣本累積中）。
- **排除**：`_IC_SKIP = {score, month, dow}`（score 另有專屬校準；月份/星期為日曆特徵、非 alpha）。
- 全部特徵**純由日K陣列或既有指標計算、無外部相依**，候選/被否決/對照組用同一支函式擷取。

## 1. 動能 / 報酬（Momentum）

| 因子 | 定義 | 做多方向假設 |
|---|---|---|
| `ret1/ret5/ret10/ret20/ret60/ret120` | 近 1/5/10/20/60/120 日報酬(%) | 中期＋（動能延續）、極短期可能反轉 |
| `chg_pct` | 當日漲跌幅(%) | ＋ |
| `gap_today` | 今日開盤相對昨收跳空(%) | ＋/情境 |
| `up_days` | 連續上漲天數（轉負記負連跌） | ＋（適度）、過高過熱 |
| `rs5/rs20/rs60` | 相對加權指數的超額報酬(%) | ＋（相對強度） |

## 2. 位置 / 乖離（Position / Distance）

| 因子 | 定義 | 做多方向假設 |
|---|---|---|
| `dist_ma5/10/20/60/120` | 距各期均線(%) | 適度＋；過度正乖離＝追高風險 |
| `dist_hi52/dist_lo52` | 距 52 週高/低(%) | 近高＝強勢（情境）、近低＝弱勢 |
| `pos52` | 52 週區間位置(0=最低,100=最高) | ＋（強勢） |
| `brk20/brk60` | 相對前 20/60 日最高(%)，>0＝已突破前高 | ＋（突破） |
| `pull10` | 距近 10 日高點回檔(%) | 回檔適度＝進場機會 |
| `ma20_slope/ma5_slope` | 20 日線 5 日 / 5 日線 3 日斜率(%) | ＋（趨勢向上） |
| `ma_stack` | 均線多頭排列強度(5>10>20>60，0~3) | ＋ |
| `above20_days` | 連續站上 20 日線天數 | ＋（趨勢持續） |

## 3. 量能（Volume / Liquidity）

| 因子 | 定義 | 做多方向假設 |
|---|---|---|
| `vol_x20/vol_ratio` | 今日量 ÷ 近 20 日均量 | ＋需配合方向（量增確認趨勢） |
| `vol_trend5` | 近 5 日均量 ÷ 前 5 日均量 | ＋ |
| `vol_z20` | 今日量相對近 20 日 z 分數 | ＋（異常放量） |
| `turnover_m` | 成交值(百萬元) | 流動性門檻（非 alpha） |
| `vol_last` | 今日成交量 | 流動性 |

## 4. 波動 / K 棒結構（Volatility / Structure）

| 因子 | 定義 | 做多方向假設 |
|---|---|---|
| `atr_pct` | ATR / 價(%) | 情境（高波動雙面） |
| `vol20/vol60_ann` | 20 日報酬標準差 / 60 日年化波動(%) | 情境 |
| `maxdd20` | 近 20 日最大回檔(%) | 越淺越＋ |
| `range_pct` | 當日高低振幅 / 價(%) | 情境 |
| `body_pct/uwick_pct/lwick_pct` | 今日 K 棒實體／上影／下影占比(%) | 長實體＋收高＝＋；長上影＝賣壓 |
| `clv` | 收盤在當日高低區間位置(%)，100＝收最高 | ＋ |

## 5. 既有指標 / 多時框 / 大盤環境

| 因子 | 定義 | 做多方向假設 |
|---|---|---|
| `raw_score/bull_score/bear_score` | 多時框原始分與多空分 | ＋（bull）/−（bear） |
| `adx_value/adx_bias` | ADX 強度與 +DI/−DI 方向 | 強度＋方向一致才＋ |
| `rsi_value` | RSI | 健康區＋、極端反轉 |
| `vol_ratio/resonance/weekly_bias` | 量比 / 多時框共振 / 週線偏向 | ＋（共振、週線偏多） |
| `ema_align/macd_bias` | EMA 排列 / MACD 偏向（類別字串） | 多頭排列、偏多＝＋ |
| `sentiment/regime` | 大盤情緒分 / Regime 趨勢（類別） | ＋（偏多環境） |

## 6. 基本面（`fund_score`，若可取得）

| 因子 | 定義 |
|---|---|
| `f_score/f_grade/f_cov` | 基本面總分 / 等級 / 覆蓋率 |
| `f_growth/f_profit/f_value` | 成長 / 獲利 / 估值 分項 |
| `rev_yoy` | 營收年增率(%) |

## 7. 現況與下一步（誠實揭露）

- 這些因子**全部 shadow-only**：只進特徵快照供事後驗證，**不改任何線上訊號分數**。
- 目前 IC 報告受限於**已結算 shadow 樣本量**（系統上線時間短、樣本累積中），多數因子可能
  仍標 `insufficient`。因子要進正式權重，須通過 IC / 分位數單調性 / 成本後 / OOS 穩定性 /
  共線性檢驗並經你核准（見 `CHANGE_CONTROL.md` 升級門檻）。
- **尚未做**（Phase 3 後續）：因子共線性/冗餘矩陣、消融（ablation）、成本後分層報告、
  Challenger 規格。待樣本足夠再推進。
