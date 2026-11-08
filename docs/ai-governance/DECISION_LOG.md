# Taiwan Signal｜決策紀錄（DECISION_LOG）

> 治理 TS-GOV-002/003。每個影響**策略/風控/部署**的決定追加一列。這是事後歸因與稽核的
> 單一事實來源。格式見表頭；新項目往**最上面**加。

| 日期 | 任務 ID | 風險 | 決定 | 理由 | 核准人 | Commit/PR |
|---|---|---|---|---|---|---|
| 2026-11-08 | TS-TEST-001 | risk:none | 新增 fail-closed 契約測試（大盤/風控資料缺失、外資暫停等級→不交易；外資三層扣分一致）；並把 run_all_checks（非live的參考程式）的外資映射補上 heavy 層與 #126 一致 | 鎖住關鍵失敗情境；消除 #126 遺漏的 run_all_checks 陳舊三層映射（dead code，不影響 config_hash） | Claude 自選（使用者授權「繼續做其他的」） | PR #128 |
| 2026-11-08 | TS-DATA-001/TS-TEST-001 | risk:none | 新增單位契約測試（test_unit_contracts：部位計算 9 項不變量＋2% 風控保證＋部位/流動性上限）與資料字典（DATA_DICTIONARY.md） | 把剛修好的單位約定硬性鎖住，未來任何改動破壞關係即 CI 紅；純測試/文件 | Claude 自選（使用者授權「繼續做其他的」） | PR #127 |
| 2026-11-08 | TS-RISK-FOREIGN | **risk:champion** | 外資賣超熔斷門檻依台股實際量能（日成交值~8,500億、外資日常±100~300億）重校為三層：warning −500億（扣8）、heavy −1,000億（扣12）、stop −1,800億（暫停新多單）；取代舊 −20/−50億（幾乎天天觸發）。Champion 版本 champion-208e598d8ffb → champion-d4eb06e13ef2 | 舊門檻失去事件意義、形同常態扣分；新門檻對照 11/2 −735億、11/5 −1,024億、10/15 −3,025億等真實分布 | **使用者明確核准**（「選B」→審閱 500/1,000/1,800 數字→「同意開始執行」） | PR #126 |
| 2026-11-08 | TS-DATA-001 | risk:none | 單位治本：資料存取邊界（get_recent/closed/pending_signals）一律補明確 `suggested_shares`（股），讀取端（含風控 portfolio_risk/plan_rescale、scanner、_position_view）改優先讀它 | 根除 `suggested_lots` 同名雙語意的 1000× 誤讀陷阱；純附加、不改既有欄位值，今日數值零變動、467 passed | Claude 自選（使用者授權「自選直接做」） | PR #124 |
| 2026-11-08 | TS-GOV-002 | risk:none | 建立治理文件（AI_ROLES / AI_WORKFLOW / CHANGE_CONTROL / DECISION_LOG）、PR 模板、`scripts/freeze_champion.py` 快照重產工具 | 把凍結＋可回溯機制正式寫進變更控管流程；純文件/工具，零決策變更 | Claude 自選（使用者授權「自選直接做」） | PR #123 |
| 2026-11-08 | TS-GOV-001 | risk:none | 每筆訊號落盤時把 `config_hash`+`strategy_version` 寫入 `raw_json`（provenance） | 讓歷史訊號可回溯到產生它的策略版本；純 metadata，不改決策、不動 schema | 使用者授權「自選直接做」 | PR #122 |
| 2026-11-08 | TS-GOV-001/003 | risk:none | Phase 0 系統基準報告 + Champion 凍結（`strategy_version.py`、`CHAMPION_SNAPSHOT.json`、`test_champion_freeze`） | 建立 Champion 變更的硬閘門與系統現況基準；零策略行為變更 | 使用者（「可以開始按計畫去進行修正」） | PR #121 |

<!-- 新增一列時：日期、任務 ID、風險標籤、決定內容、理由、核准人、對應 PR/commit。
     risk:champion 與 risk:infra 的項目必須有明確的使用者核准來源。 -->
