# Taiwan Signal｜變更控管（CHANGE_CONTROL）

> 對應雙 AI 交接文件 §5、§9 與 Phase 1（TS-GOV-003）。本檔定義 Champion 正式策略如何被
> 保護、什麼變更需要使用者核准、如何升級、如何回滾。角色見 `AI_ROLES.md`，流程見
> `AI_WORKFLOW.md`。

最後更新：2026-11-08

---

## 1. Champion 是什麼、被什麼保護

**Champion＝目前決定線上訊號行為的規則式策略**（入口 `signal_engine.generate_signal_tw`，
參數來源 `config.py`；`learning_model` 的模型 champion 為 **shadow-only**，不碰正式決策，
見 `SYSTEM_BASELINE.md` §3/§5）。

三道保護：

1. **凍結（freeze）— TS-GOV-003**
   - `strategy_version.py` 把被追蹤的策略/風控常數（`_TRACKED`）集中算出穩定的
     `config_hash` 與 `strategy_version`（排除金鑰、使用者資金、時區/假日等非決策項）。
   - `docs/ai-governance/CHAMPION_SNAPSHOT.json` 凍結目前常數。
   - `tests/test_champion_freeze.py`：任何人改到被追蹤常數 → `config_hash` 與快照不符 →
     **CI 轉紅**，逼出一個有意識的 PR。這就是變更控管的硬閘門。

2. **可回溯（provenance）— TS-GOV-001**
   - 每筆訊號落盤時（`state_store.save_signal`）把當下的 `config_hash` + `strategy_version`
     寫進 `raw_json`。任何歷史訊號都能回答「這是哪一版策略產生的」。

3. **風控獨立（fail-closed）**
   - `risk_manager` 在資料不可用時阻擋，**AI 不得繞過**（見 `AI_ROLES.md` §2）。

## 2. 什麼變更需要使用者核准

| 變更 | 需核准？ | 為什麼 |
|---|---|---|
| 改 `_TRACKED` 內任一常數（如 `MAX_RISK_PER_TRADE`、`SIGNAL_THRESHOLDS`…） | ✅ | 改變線上決策行為 |
| 把 Challenger／shadow 模型升為 Champion | ✅ | 升級門檻見 §4 |
| 部署、金鑰、Render 設定、排程/資料庫結構 | ✅ | 影響正式環境 |
| 接券商、開啟實際下單 | ✅ | 最高風險 |
| 新增研究/測試/文件/顯示端點（不碰決策） | ❌ | 標 `risk:none`/`risk:research` |

## 3. 核准變更 Champion 常數的標準步驟

1. 在**獨立分支**改 `config.py` 的常數，commit（標 `risk:champion`）。
2. 跑 `python scripts/freeze_champion.py --check` 看差異；CI 此時會因快照不符而紅（預期）。
3. **取得使用者明確核准**（記入 `DECISION_LOG.md`）。
4. 核准後重新產生快照：`python scripts/freeze_champion.py`，commit 更新後的
   `CHAMPION_SNAPSHOT.json`。CI 轉綠。
5. PR 依 `.github/pull_request_template.md` 附：diff、測試、（適用時）回測/OOS、rollback。

> 未經步驟 3 的核准就重產快照＝繞過閘門，**禁止**。快照更新必須與被核准的常數變更同一個 PR。

## 4. 升級門檻（Challenger → Production）

依序不得跳關：**Backtest → Walk-Forward/OOS → Shadow → Paper → 雙方審查 → 使用者核准**。

升級證據包需含：研究假設與定義、commit/diff、資料來源與 point-in-time 證明、
單元/整合/回歸測試、樣本內與 OOS、成本敏感度、多空與分層結果、樣本數與信賴區間、
風險與失效情境、Shadow/Paper 報告、研究審查、工程審查、使用者核准、rollback 版本與步驟。

**阻止升級**的情況（任一即停）：關鍵資料缺失/延遲/衝突、回測與 Shadow/Paper 不一致、
成本超標、模型漂移或校準失效、風控資料不可用、CI 失敗、未來資料洩漏、重大樣本偏誤、
未授權修改 Champion。

> 現況：Paper 與券商閉環**未建**（`SYSTEM_BASELINE.md` §5）。在其建立前，升級鏈最多走到
> Shadow，不得宣稱可上實盤。

## 5. 回滾

- 凍結/可回溯/文件類變更：`git revert` 對應 commit 即可（純新增，無既有邏輯改動）。
- Champion 常數變更：還原 `config.py` 常數 + 還原 `CHAMPION_SNAPSHOT.json` 到前一版，
  跑 `test_champion_freeze` 確認綠燈。
- 變更前先確認可回滾版本存在；變更後跑回歸測試。
