# Taiwan Signal｜決策紀錄（DECISION_LOG）

> 治理 TS-GOV-002/003。每個影響**策略/風控/部署**的決定追加一列。這是事後歸因與稽核的
> 單一事實來源。格式見表頭；新項目往**最上面**加。

| 日期 | 任務 ID | 風險 | 決定 | 理由 | 核准人 | Commit/PR |
|---|---|---|---|---|---|---|
| 2026-11-08 | TS-REGIME-001 | risk:none | Regime baseline 現況與增量價值方法（REGIME_STATUS.md）：確認 regime.py 為可解釋規則 baseline、shadow-only 未接決策；regime/sentiment 已入 shadow 特徵快照故 factor_ic_report 已在量測其關聯；載明策略路由需足量 OOS+核准（risk:champion）才可接決策 | 完成「不接正式策略、先 OOS」；誠實標註樣本不足、過度擬合風險 | Claude 自選（使用者「未動的部分要記得動工」） | PR #133 |
| 2026-11-08 | TS-SHORT-001 | risk:none | 多空獨立性稽核（LONGSHORT_SEPARATION_AUDIT.md）：確認訓練/IC/校準/方向維度統計/回測/成本皆依 direction 分開、未混用；標出唯一混合＝headline「候選(多空混合) vs 對照組(隨機做多)」比較，提兩案建議（risk:research，待你定奪） | 完成「多空不混合訓練與績效推論」核對；誠實標出 headline 混淆與放空樣本不足 | Claude 自選（使用者「未動的部分要記得動工」） | PR #132 |
| 2026-11-08 | TS-FACTOR-001 | risk:none | 新增 FACTOR_REGISTRY.md：把 shadow.snapshot() 記錄的 ~50 個因子分 7 類逐一登錄（定義/類別/做多方向假設/來源），載明 factor_ic_report 的 IC/分位數/單調性/BH-FDR 驗證機制與樣本門檻 | Phase 3 因子研究標準化起步；誠實標註全 shadow-only、樣本累積中、未進正式權重 | Claude 自選（使用者「未動的部分要記得動工」） | PR #131 |
| 2026-11-08 | TS-DATA-PIT-audit | risk:none | Look-ahead 稽核（程式碼追蹤，因沙箱無法連 yfinance 跑回測）：live（盤後產訊號、T+1 進場、用已公告法人資料）無 look-ahead；backtest 已具 PIT 紀律（價格截斷/週線 as-of/TWII regime 歷史重建/總經 point-in-time）。結論：無實質 look-ahead，**PIT B/C 不具充分投資理由**，維持 A | 用證據決定不投入大工程，資源轉因子/多空研究 | Claude 自選（使用者「自己安排進度」） | PR #130 |
| 2026-11-08 | TS-DATA-PIT-A | risk:none | PIT 起步 A：新增 data_quality.py（品質狀態列舉＋worst/is_tradeable/classify_freshness 純函式），把 get_system_status 的資料可用性布林升級為標準化品質狀態（純顯示、can_trade 行為不變）；並產出 PIT_FEASIBILITY.md（B/C 可行性＋A/B 量化測試設計） | 標準化 fail-closed 語意、為 PIT 鋪路；誠實評估 C 受限於無歷史快照 | 使用者「選A」＋要求評估 PIT 可行性與實測差異 | PR #129 |
| 2026-11-08 | TS-TEST-001 | risk:none | 新增 fail-closed 契約測試（大盤/風控資料缺失、外資暫停等級→不交易；外資三層扣分一致）；並把 run_all_checks（非live的參考程式）的外資映射補上 heavy 層與 #126 一致 | 鎖住關鍵失敗情境；消除 #126 遺漏的 run_all_checks 陳舊三層映射（dead code，不影響 config_hash） | Claude 自選（使用者授權「繼續做其他的」） | PR #128 |
| 2026-11-08 | TS-DATA-001/TS-TEST-001 | risk:none | 新增單位契約測試（test_unit_contracts：部位計算 9 項不變量＋2% 風控保證＋部位/流動性上限）與資料字典（DATA_DICTIONARY.md） | 把剛修好的單位約定硬性鎖住，未來任何改動破壞關係即 CI 紅；純測試/文件 | Claude 自選（使用者授權「繼續做其他的」） | PR #127 |
| 2026-11-08 | TS-RISK-FOREIGN | **risk:champion** | 外資賣超熔斷門檻依台股實際量能（日成交值~8,500億、外資日常±100~300億）重校為三層：warning −500億（扣8）、heavy −1,000億（扣12）、stop −1,800億（暫停新多單）；取代舊 −20/−50億（幾乎天天觸發）。Champion 版本 champion-208e598d8ffb → champion-d4eb06e13ef2 | 舊門檻失去事件意義、形同常態扣分；新門檻對照 11/2 −735億、11/5 −1,024億、10/15 −3,025億等真實分布 | **使用者明確核准**（「選B」→審閱 500/1,000/1,800 數字→「同意開始執行」） | PR #126 |
| 2026-11-08 | TS-DATA-001 | risk:none | 單位治本：資料存取邊界（get_recent/closed/pending_signals）一律補明確 `suggested_shares`（股），讀取端（含風控 portfolio_risk/plan_rescale、scanner、_position_view）改優先讀它 | 根除 `suggested_lots` 同名雙語意的 1000× 誤讀陷阱；純附加、不改既有欄位值，今日數值零變動、467 passed | Claude 自選（使用者授權「自選直接做」） | PR #124 |
| 2026-11-08 | TS-GOV-002 | risk:none | 建立治理文件（AI_ROLES / AI_WORKFLOW / CHANGE_CONTROL / DECISION_LOG）、PR 模板、`scripts/freeze_champion.py` 快照重產工具 | 把凍結＋可回溯機制正式寫進變更控管流程；純文件/工具，零決策變更 | Claude 自選（使用者授權「自選直接做」） | PR #123 |
| 2026-11-08 | TS-GOV-001 | risk:none | 每筆訊號落盤時把 `config_hash`+`strategy_version` 寫入 `raw_json`（provenance） | 讓歷史訊號可回溯到產生它的策略版本；純 metadata，不改決策、不動 schema | 使用者授權「自選直接做」 | PR #122 |
| 2026-11-08 | TS-GOV-001/003 | risk:none | Phase 0 系統基準報告 + Champion 凍結（`strategy_version.py`、`CHAMPION_SNAPSHOT.json`、`test_champion_freeze`） | 建立 Champion 變更的硬閘門與系統現況基準；零策略行為變更 | 使用者（「可以開始按計畫去進行修正」） | PR #121 |

<!-- 新增一列時：日期、任務 ID、風險標籤、決定內容、理由、核准人、對應 PR/commit。
     risk:champion 與 risk:infra 的項目必須有明確的使用者核准來源。 -->
