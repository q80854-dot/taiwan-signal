# Taiwan Signal｜系統基準報告（Phase 0 / SYSTEM_BASELINE）

**產出日期：** 2026-11-08（Asia/Taipei）
**產出者：** Claude（量化工程/實作）
**對應：** 雙 AI 協作交接文件 v1.0 §11「Claude 第一輪必須回覆的內容」、分階段計畫 Phase 0

> 本報告是「現況盤點」，不是實盤績效、不是 Production 稽核證明。所有數字只作研究線索；
> 未知項目一律標 UNKNOWN / 未建置。本報告為**純文件**，不修改任何程式、策略、風控或部署。

---

## 1. 最新 main commit

- Repository：`q80854-dot/taiwan-signal`，branch `main`
- **SHA：`e83403ca55831fcf9627cfa031d73faf6d8ac693`**（2026-11-08，與交接文件基準一致）
- 測試：`python -m pytest -q` 收集 **458 個測試**（見 §8）

---

## 2. 模組與資料流地圖

### 2.1 正式訊號流（Production signal path）

```
排程 job_daily_scan（平日 16:30）
  → scanner.TWScanEngine.run_scan()
     → stock_universe.build_universe()                 # 掃描池
     → 逐檔 data_fetcher.fetch_ohlcv() + indicators     # 日/週/時多週期技術指標
     → signal_engine.generate_signal_tw()               # ★ Champion 訊號產生（評分、停損停利、部位）
        → event_filter.check()                          # 除權息擋單 / 重大訊息扣分
        → risk_manager.run_all_checks()                 # 風控閘門（fail-closed）
     → scanner 篩選/排序/現金與風險預算壓縮(fit_to_cash)
     → shadow.*（candidate/rejected/control 平行記錄，不影響推播）
     → telegram_bot.push_signal()                       # 逐檔推播（回傳 any_ok 供送達對帳）
     → state_store.save_signal()                        # 寫入 signals 表（待結算）
  → 盤後 telegram_bot.send_daily_report()
```

### 2.2 待結算 → 績效（Live performance tracking）

```
scanner._resolve_pending_signals()（結算路徑）
  → backtester.simulate_staged_exit()  → execution.simulate_staged_exit()   # ★ 分批出場（共用）
  → state_store.update_signal_result()  # 寫回 signals 表 result/pnl
```

### 2.3 研究/回測/學習流

```
backtester.backtest_symbol_tw / walk_forward_backtest_tw / portfolio_backtest_tw
  → execution.simulate_staged_exit()（分批）, calc_tw_pnl（成本：手續費/稅/滑價/借券）

shadow.resolve_pending()
  → execution.simulate_exit()（單次出場，供 R 標籤）  # 與回測刻意不同（見 §5）

learning_model.build_dataset(direction="buy") → train_and_eval / run_challengers
  → 時序 walk-forward（expanding）、Purged+Embargo CV、獨立 holdout
  → META_KEY="learning_model_v1" champion（★ shadow-only，不進線上決策）

learning.* / regime.*  → 研究統計（因子 IC/分位數、分數校準、regime baseline），研究/顯示用
```

---

## 3. Champion 正式策略：入口、參數來源、呼叫路徑

- **線上「Champion 策略」＝規則式訊號引擎**，入口 `signal_engine.generate_signal_tw()`（`signal_engine.py:314`）。
  由 `scanner.py` 編排呼叫；參數來自 `config.py`（`THRESH`、`SWING_PARAMS`、`CIRCUIT_BREAKER`、
  成本率、風險參數等）。**沒有**獨立的「策略版本號/參數 hash」凍結機制（見 §9 凍結方案）。
- **「Champion 學習模型」**＝`learning_model` 的 `META_KEY="learning_model_v1"`（`learning_model.py:27`）。
  **shadow-only**：只記錄 `model_prob` 供顯示/研究，**不參與**訊號產生、停損、部位或風控。
  `promote_champion()`（`learning_model.py:622`）存在但需人工呼叫、不自動晉升。
- 風控 `risk_manager`（`run_all_checks` / `check_daily_loss_limit` / `check_margin_usage`）為獨立層，
  資料不可用時 fail-closed（擋新訊號），訊號引擎不得繞過。

**關鍵釐清**：線上決策是**規則式**的，不是 AI 模型。先前「誠實 AUC ~0.41」是那個 shadow-only 模型的研究結果，與線上推播無關。

---

## 4. 七個核心模組：職責與依賴

| 模組 | 職責 | 主要依賴 | 備註 |
|---|---|---|---|
| `signal_engine.py` | 訊號產生、評分、停損停利、部位（`calc_position_size`/`fit_to_cash`/`calc_stop_loss_tw`/`calc_trade_cost`） | config、indicators、event_filter、risk_manager | 線上 Champion 入口 |
| `risk_manager.py` | 單筆/當日/組合風險硬限制、fail-closed | config、state_store | 獨立層，AI 不得繞過 |
| `backtester.py` | 回測、WFO、組合回測、成本（`calc_tw_pnl`） | execution（分批出場）、scoring_engine | 回測核心 |
| `shadow.py` | candidate/rejected/control 研究樣本追蹤與結算 | execution（單次出場）、state_store | R 標籤口徑 |
| `execution.py` | 共用出場模擬：`simulate_exit`（單次）、`simulate_staged_exit`（分批，pnl_fn 注入） | 無（純函式） | #114/#119 建立並收斂 |
| `learning_model.py` | 離線模型訓練/評估、時序 WFO、Purged+Embargo、holdout | numpy、shadow | shadow-only，buy-only（#120） |
| `regime.py` | 市場狀態 baseline（趨勢/波動/流動性） | data_fetcher | 研究/顯示，不接線上 |

---

## 5. 交易路徑差異矩陣（Live / Backtest / Shadow / Paper）

| 面向 | 正式訊號追蹤（signals 表） | Backtest | Shadow（R 標籤） | Paper |
|---|---|---|---|---|
| 進場價 | 訊號日後首根開盤（概念） | 訊號日後首根開盤 | 訊號日後首根開盤 | **未建置** |
| 出場模型 | **分批**（staged，經 `execution.simulate_staged_exit`） | **分批**（同一實作） | **單次**（`execution.simulate_exit`） | — |
| 成本 | `calc_tw_pnl`（費/稅/滑價/借券） | `calc_tw_pnl` | resolve_pending 內同口徑（含借券） | — |
| 同 K SL/TP | 先停損（保守） | 先停損（保守） | 先停損（保守） | — |
| 跳空穿越停損 | 開盤成交 | 開盤成交 | 開盤成交 | — |

**結論**：
- 正式追蹤與回測**用同一套分批出場（execution）**，#119 後已收斂、口徑一致。
- Shadow 用**單次出場**供 R 標籤，與回測**刻意不同模型**（R 歸因 vs 使用者實際分批計畫）。此差異是設計選擇，非 bug，但做「shadow R」vs「回測淨值」比較時須知口徑不同。
- **Paper 層未建置**（無 `paper_filled` / 模擬成交回報閉環）。**Live/券商成交亦未建置**。
- `sent`≠`delivered`：已於 #111 建立對帳（`delivered` 欄＋`push_signal` any_ok），`sent=1 且 delivered=0` 可查。

---

## 6. 資料表、API、排程、外部資料源

### 6.1 資料庫（SQLite 本機測試 / PostgreSQL 生產；`state_store.py`，共 24 張表）

重點表與欄位：
- `signals`：id、ticker、direction、score、grade、entry_price、stop_loss、tp1/2/3、`suggested_lots`（**實存股數**，見 §7）、risk_twd、risk_pct、position_value、result、pnl_twd、status、generated_at、raw_json。
- `shadow_signals`：bar_date、ticker、kind（candidate/rejected/control）、direction、score、entry、stop、tp1/2/3、features(JSON)、status、result、r_multiple、mfe_r/mae_r、r1/r5/r10/r20、sent、`delivered`/`delivered_at`（#111）、`feat_schema_ver`（#118）、model_prob。
- 其他：scan_history、meta、subscribers、member/member_watch/member_prefs、official_volume、inst_daily、shadow_runs 等。

### 6.2 API（Flask，`app.py`）— 研究/顯示重點

- 線上狀態：`/api/state`（每頁輪詢）、`/api/signals`、`/api/market`、`/api/regime`（#119）。
- 研究（站主專用）：`/api/learning`、`/api/learning/score_calibration`（#113）、`/api/learning/factor_ic`（#117，含分位數 #120）、`/api/admin/shadow`（含送達率 #115）。
- 財經日曆：`/api/calendar`（econ_calendar，#116 公布時段短快取＋FRED 官方校正）。
- 權限：`members.gate` 登入牆（`/api/*`）；站主端點 `owner_required`+`admin_required`；寫入需 `X-TS`。

### 6.3 排程（APScheduler，約 39 個 job）

掃描/報告：premarket_scan、daily_scan(16:30)、scan_watchdog(17:00)、premarket/postmarket_report、morning_brief。
盤中：intraday_check、intraday_snapshot、final_bars。
資料預熱：market_overview_warm(4min,#116)、world_warm、finmind_warm、econ_warm、warm_instrument、refresh_universe。
維運：health_watch(交易時段每2h,#109)、boot_restart_notice(#109)、pre_scan_restart(16:15 OOM 防護)、learning_backup、typhoon_watch、freshness_poll、revenue_watch、news_watch。
全域排程錯誤告警 listener（EVENT_JOB_ERROR，#109）。

### 6.4 外部資料源

TWSE（官方加權指數日收/FMTQIK 成交/MOPS 財報法人）、TPEX（上櫃）、FinMind（財報/法人）、Fugle（即時）、yfinance（備援，常 401/403）、FMP（財經日曆 actual）、FRED（財經官方校正）、CoinGecko（BTC/黃金）、Telegram（推播）、Gemini/Groq/Anthropic（可選 AI 報告，連不上只 warn，#108）。

---

## 7. `suggested_lots` 股數/張數語意

**已於 #112 完整稽核**（詳見 `docs/UNIT_AUDIT_2026-11-06.md`）。摘要：

- **DB `signals.suggested_lots` 欄位名為「lots」但實際存「股數」**（歷史遺留命名）。
- 同名雙語意：記憶體訊號 dict 的 `suggested_lots`＝張數（另有 `suggested_shares`＝股數）；
  DB 列/`/api/signals` 的 `suggested_lots`＝股數（無 suggested_shares）；`/api/state` 的 `_db_today_signals`
  已換算成張數並補 `suggested_shares`。
- **已修兩個顯示層 1000× bug**（#112）：`explain.py` 訊號解釋、dashboard 歷史訊號卡。
- **部位/風險計算本身單位一致、無誤**（`calc_position_size` 全程以股為基準）。
- **待你核准的治本重構**（未做）：在 `get_recent_signals`/`get_closed_trades` 統一正規化，根除來源依賴。

---

## 8. 測試

- 命令：`python -m pytest -q`（CI：`.github/workflows/tests.yml`，push/PR 觸發）。
- 數量：**458 個測試**；本機 `-p no:randomly` 確定性排序全綠。
- **已知 flake（與功能無關）**：
  1. 外網相依測試在 sandbox 偶發（yfinance `^NDX` 等 CONNECT 403/逾時）——時序性、重跑即綠。
  2. `test_members.py::test_watchlist_views` 在某些隨機排序下因**共用 temp DB 跨測試隔離**偶發失敗——存在於 main、與近期改動無關。
- **未覆蓋風險（誠實揭露）**：
  - backtester 回測結果的**真實市場正確性**無法在 sandbox 驗證（需真實 OHLCV 前後比較）。
  - **Paper 層不存在**，無對應測試。
  - 線上推播端到端（真正送達使用者手機）無自動測試，只有 send_message 回傳對帳。

---

## 9. Champion 凍結方案（建議，未實作，待核准）

目前**沒有**正式策略的唯讀快照/版本號/參數 hash。建議（Phase 0 交付、需你核准後才做）：

1. 新增 `strategy_snapshot`：將 `config.py` 的訊號/風控相關常數（THRESH、SWING_PARAMS、CIRCUIT_BREAKER、
   風險參數、成本率）序列化，計算 `config_hash`，存檔為唯讀基準並標版（如 `champion-v1-<sha>`）。
2. 每筆 `signals` 記錄當時 `config_hash` 與 code commit SHA（metadata），使每筆訊號可回溯到產生它的策略版本。
3. 任何 `config.py` 策略/風控常數變更，CI 檢查 `config_hash` 是否變動並要求 PR 標註＋你核准。
4. Challenger 一律用獨立 namespace（研究程式不得寫入 `signals`/正式風控 meta）——現況 shadow/learning 已是獨立 namespace，符合此原則。

> 在你核准前，我**不**建立此快照機制、也不改 `config.py`。

---

## 10. Phase 0 交付物、位置、驗收

| 交付物 | 位置 | 狀態 |
|---|---|---|
| 系統基準報告 | `docs/ai-governance/SYSTEM_BASELINE.md`（本檔） | ✅ 本 PR |
| 單位稽核 | `docs/UNIT_AUDIT_2026-11-06.md` | ✅ 已存在（#112） |
| 模組/資料流圖 | 本報告 §2–§6 | ✅ |
| 策略參數登錄 | 本報告 §3；完整凍結待 §9 核准 | 🟡 部分 |
| 差異矩陣 | 本報告 §5 | ✅ |
| 缺陷/風險清單 | 本報告 §8、§11 | ✅ |
| Champion 快照 | §9 方案，待核准 | ⬜ 未做 |

**驗收**：核心路徑可追溯（§2–§5）、正式策略入口與參數來源明確（§3）、未知項目標示（§11）、**不修改策略、不部署**（本 PR 僅新增文件）。

---

## 11. 無法確認 / 需你決策的事項

**UNKNOWN / 未驗證（需真實資料或 Production 存取）：**
- Render 部署的實際 commit 是否＝main 最新（部署由 push 觸發，推定一致但未稽核）。
- Production 用 SQLite 或 PostgreSQL 的實際連線狀態（sandbox 無法查 Render DB）。
- 回測結果是否對真實市場正確（需真實 OHLCV 前後比較）。
- 放空是否有真實券源/借券費率/可成交（系統明確標示「無此資料、放空為研究版/風險提醒」）。

**未建置（不可宣稱已完成）：**
- Paper 模擬成交與回報閉環、券商/實盤下單。
- 完整 Feature Store、PIT 歷史資料庫（財報/法人/新聞逐欄 available_time）、Strategy Router、
  多模型上線、組合 CVaR/VaR、全球市場適配器。

**需你決策：**
1. §9 Champion 凍結機制是否建立（建議建，低風險）。
2. 樣本量：目前 shadow 約 20 交易日，統計上尚不足以支撐多模型/regime 路由上線——是否「先累積樣本、備好量測」，或你願提供真實回測資料做等價＋WFO。
3. `suggested_lots` 治本重構是否進行（§7）。
4. 雙 AI 工作流程文件（AI_ROLES.md/AI_WORKFLOW.md/CHANGE_CONTROL.md 等）是否要我一併建立。

---

## 附：近期已落地的規格相關工作（可追溯 commit）

資料可信度：MAE/MFE/R 爆量修復＋清洗(#110)、sent→delivered 對帳(#111)、送達率(#115)、feature schema 版本(#118)。
單位：稽核報告＋修 1000×(#112)。
研究層：分數↔淨R 校準(#113)、因子 Rank IC(#117)＋分位數(#120)、多空標籤拆分(#120)。
執行一致性：共用出場模擬器 execution.py（shadow #114、backtester #119）。
Regime：可解釋 baseline(#119)。
維運：告警完善(#109)、財經日曆正確性＋載入加速(#116)、可選 AI 來源告警降級(#108)。
