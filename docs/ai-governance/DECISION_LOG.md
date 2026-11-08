# Taiwan Signal｜決策紀錄（DECISION_LOG）

> 治理 TS-GOV-002/003。每個影響**策略/風控/部署**的決定追加一列。這是事後歸因與稽核的
> 單一事實來源。格式見表頭；新項目往**最上面**加。

| 日期 | 任務 ID | 風險 | 決定 | 理由 | 核准人 | Commit/PR |
|---|---|---|---|---|---|---|
| 2026-11-08 | TS-GOV-002 | risk:none | 建立治理文件（AI_ROLES / AI_WORKFLOW / CHANGE_CONTROL / DECISION_LOG）、PR 模板、`scripts/freeze_champion.py` 快照重產工具 | 把凍結＋可回溯機制正式寫進變更控管流程；純文件/工具，零決策變更 | Claude 自選（使用者授權「自選直接做」） | PR #123 |
| 2026-11-08 | TS-GOV-001 | risk:none | 每筆訊號落盤時把 `config_hash`+`strategy_version` 寫入 `raw_json`（provenance） | 讓歷史訊號可回溯到產生它的策略版本；純 metadata，不改決策、不動 schema | 使用者授權「自選直接做」 | PR #122 |
| 2026-11-08 | TS-GOV-001/003 | risk:none | Phase 0 系統基準報告 + Champion 凍結（`strategy_version.py`、`CHAMPION_SNAPSHOT.json`、`test_champion_freeze`） | 建立 Champion 變更的硬閘門與系統現況基準；零策略行為變更 | 使用者（「可以開始按計畫去進行修正」） | PR #121 |

<!-- 新增一列時：日期、任務 ID、風險標籤、決定內容、理由、核准人、對應 PR/commit。
     risk:champion 與 risk:infra 的項目必須有明確的使用者核准來源。 -->
