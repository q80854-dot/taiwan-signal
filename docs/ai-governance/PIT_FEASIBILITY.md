# Taiwan Signal｜Point-in-Time（PIT）可行性評估與測試計畫

> 治理 Phase 2。回答使用者問題：PIT 是否可行？B/C 版本怎麼在「實際測出改前改後差異」之後
> 再決定採用哪版。本檔只做評估與測試設計，不改資料路徑。相關：`DATA_DICTIONARY.md §5`、
> `SYSTEM_BASELINE.md`。

最後更新：2026-11-08

---

## 1. 為什麼要 PIT

避免 **look-ahead bias（未來資料洩漏）**：回測/研究若用到「當時其實還拿不到」的資料
（財報發布前就知道數字、收盤後才定案的法人買賣超卻當盤中可用…），績效會虛高、實盤卻跟不上。
PIT＝每筆資料都記「event_time（事件發生）」與 **available_time（當時真正可取得）**，
研究/回測一律以 available_time 對齊。

## 2. 現況盤點（可行性基礎）

| 面向 | 現況 | 對 PIT 的意義 |
|---|---|---|
| 訊號 provenance | 已有 `config_hash`/`strategy_version`（#122） | ✅ 版本可回溯這塊已就緒 |
| 資料可用性 | 風控有 `data_available` 布林＋fail-closed（#128）；**A 已升級為品質列舉**（`data_quality.py`） | ✅ 品質狀態框架就緒 |
| 時間戳 | 多數抓取**只存「值」，不存 available_time/ingested_time** | ⚠️ 這是 PIT 最大缺口 |
| 法人買賣超 `inst_daily` | DB 有 `trade_date`，無 `available_time`/`revision_time` | ⚠️ 需加欄位（migration）才能做歷史 PIT |
| 歷史回測 | 用 yfinance 當下抓的 K 棒（已收盤定案），**無歷史快照** | ⚠️ 真 PIT 回測需 point-in-time 資料庫，本專案沒有 |

**結論：A（品質狀態標準化）已可行且已完成。B 可行。C（歷史 PIT 回測重建）在「沒有歷史快照資料庫」的前提下只能做『前向累積』，無法回溯重建過去，屬大工程且價值受限於未來資料累積。**

## 3. B / C 定義與工程量

### B：抓取層加「可取得時間＋品質」envelope（可行，建議先做）
- 在價格/法人/總經三類抓取回傳加 `fetched_at`、`available_time`（估：收盤資料=當日收盤時刻；盤中=抓取時刻）、`quality_status`。
- 風控/顯示讀 envelope，`STALE/MISSING` 時走既有 fail-closed。
- 工程量：中（動 `data_fetcher` 多處＋讀取端），**不需 DB migration**（envelope 存記憶體/raw_json）。
- 風險：risk:research（動即時資料路徑，但有契約測試＋fail-closed 測試護欄）。

### C：全面 PIT（歷史快照 + inst_daily 欄位 + 回測重建）
- `inst_daily` 等 DB 表加 `available_time`/`revision_time`/`data_version`（**schema migration，Postgres+SQLite**）。
- 從**現在起**每日快照法人/總經資料（前向累積 PIT 資料庫）。
- 回測改以 available_time 對齊。
- 工程量：大；風險：risk:infra（migration）；**關鍵限制：過去的資料無法事後重建 available_time，只能從導入日起累積**。

## 4. 怎麼「實際測出改前改後差異」（使用者要求）

PIT 的差異＝「有沒有用到未來資料」。要量化它，用**同一套回測**跑兩種對齊方式、比指標：

### 實驗設計（B 的 A/B 測試，可離線跑）
1. **控制組（現狀）**：現行 `backtester.simulate_staged_exit` 回測結果（已存在的指標：勝率、平均 R、Profit Factor、最大回撤、平均持有天數）。
2. **實驗組（PIT 對齊）**：同一批訊號、同一段期間，但把「法人買賣超／總經」這類**收盤後才定案**的輸入，改成「訊號產生當下只能用前一交易日的值」（available_time 對齊 T-1）。
3. **比較指標差異**：若兩組績效明顯不同（例如實驗組勝率/平均 R 下降），代表現行回測**確實吃到了未來資料**，PIT 有實質價值、值得做 B/C；若差異在雜訊內，代表現行對齊已夠乾淨，PIT 可降優先。
4. 產出 `PIT_AB_REPORT`：兩組指標並列＋差異％＋樣本數＋信賴區間（用既有 `learning._boot_ci`）。

> 這個 A/B 本身是 **risk:research、可離線**，不動 live。**建議下一步就先做這個量化實驗**——
> 先用數據證明 PIT 到底有沒有改變績效，再決定投入 B 或 C。先別急著改資料路徑。

## 5. 建議路線（已依 §6 稽核更新）

1. **A（已完成）**：品質狀態標準化。
2. ~~PIT A/B 量化實驗~~ → 因沙箱無法連 yfinance 跑真實歷史回測，改以**程式碼 look-ahead 稽核**
   直接回答「回測/訊號有沒有吃未來資料」（見 §6）。
3. **結論：目前無實質 look-ahead，PIT B/C 不具充分投資理由；維持 A，資源轉向因子/多空研究。**

## 6. Look-ahead 稽核結果（2026-11-08，程式碼追蹤）

沙箱不能連 yfinance 跑歷史回測，故改以程式碼追蹤判定是否存在未來資料洩漏。結論如下。

### 6.1 Live（實盤）路徑：無 look-ahead ✅
- 盤後 **16:30** 掃描（`scanner.run_daily_scan` mode="close"）→ 產生訊號 → **隔日開盤**才進場
  （`scanner.py:569`「訊號收盤後才產生，最早只能隔天開盤進場」）。
- 用到的法人買賣超（`fetch_institutional_flow`）是 T0 盤後已公告值，在 16:30 決策當下**確實可取得**；
  進場在 T+1。故不構成 look-ahead。

### 6.2 Backtest（回測）路徑：已具 PIT 紀律 ✅（非本次才補）
開發先前已針對未來資料洩漏做了多處處理：
- **價格截斷** `_truncate_ohlcv_to_date`：驗證區間外的未來價格先截掉，避免用未來價強制平倉。
- **週線 as-of** `_weekly_asof`：每根日 K 只納入「起始日 ≤ 當日」的週線，排除尚未走完的本週 K。
- **TWII regime 歷史重建** `_fetch_twii_regime_series`：逐日重建、非把「現在」套回整段歷史。
- **總經 point-in-time** `_macro_score_adj` + `_fetch_twii_change_map`/`_fetch_margin_chg_map`：
  考慮申報時間差、不看未來。

### 6.3 唯一的 PIT 相關落差：保守、非 look-ahead
- 回測**不套用**個股法人買賣超 ±5 分（`signal_engine` live 有，backtest 直接呼叫
  `check_multi_timeframe_tw` 技術面、未帶 inst）。這讓回測**低估**（而非高估）live 行為，
  方向保守，不是「偷看未來」。
- 另有一個**成交時點差異**（非 PIT 資料問題）：回測在**訊號當根 K 收盤價**進場，live 在
  **T+1 開盤**進場——回測略早一根、可能偏樂觀。屬模型化選擇，已記此處供日後若要對齊再評估。

### 6.4 判定
**無證據顯示存在會被 PIT B/C 修正的實質 look-ahead。** 既有回測對「價格/週線/總經」三類
關鍵輸入都已 point-in-time 對齊；live 用的是決策當下已公告的資料。故：
- **不建議現在投入 PIT B/C**（工程量大、C 還需 migration＋前向累積，但換得的 look-ahead 修正≈0）。
- 維持 **A**（品質狀態標準化，已上線）。
- 若日後要把回測「成交時點」改成 T+1 開盤以更貼近 live，那是獨立的回測保真度議題，與 PIT 無關。
