<!-- 治理 TS-GOV-003：每個 PR 請填下列欄位。規則見 docs/ai-governance/AI_WORKFLOW.md -->

## 風險標籤
<!-- 擇一，不確定往高一級：risk:none / risk:research / risk:champion / risk:infra -->
- 風險等級：
- 任務 ID／核准紀錄：<!-- risk:champion 與 risk:infra 需附使用者核准，並記入 DECISION_LOG.md -->

## 目的與範圍
<!-- 這個 PR 要解決什麼、改動邊界到哪 -->

## 受影響檔案 / API / 資料表
<!-- 列出主要異動點；若碰到 Champion 決策路徑請明講 -->

## diff 摘要
<!-- 關鍵改動的重點說明 -->

## 測試與 CI
- 本機測試：
- CI（pytest）：
- （適用時）回測 / OOS / 資料洩漏檢查：

## 安全 / 效能影響
<!-- 無則寫「無」 -->

## Rollback 步驟
<!-- 怎麼安全回退；Champion 常數變更需同時還原 CHAMPION_SNAPSHOT.json -->

---
<!-- 落實：不碰 Champion 決策行為的 PR 標 risk:none/research；碰到被追蹤常數（strategy_version._TRACKED）需使用者核准＋重產快照（python scripts/freeze_champion.py），見 docs/ai-governance/CHANGE_CONTROL.md -->
