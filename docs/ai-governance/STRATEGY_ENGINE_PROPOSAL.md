# 策略引擎 + Regime 路由｜設計提案（STRATEGY_ENGINE_PROPOSAL）

> 依第二階段審查 §三 3.1。治理 risk:research（階段 1–4）／risk:champion（階段 5 路由）。
> 本文件為多 PR 工程的共同依據與治理依據；每個策略上線皆對照本文件。

## 0. 原則（不可違反）
- 階段 1–4 **純 shadow**：不接掃描決策、不影響實際發出的線上訊號、不碰 Champion、config_hash 不變。
- 階段 5（Regime 路由接入線上）＝ **risk:champion**，需足量 OOS ＋ 跨 ≥2–3 獨立行情窗口證據 ＋ **使用者明確核准**；會改 config_hash（重產 CHAMPION_SNAPSHOT）。
- 空方策略（空延續/反彈失敗）等券源可得性/借券費模型（Q01）補好再做，否則空方績效不可信。
- 出場/部位/成本一律走既有共用函式（`signal_engine.calc_stop_loss_tw`/`calc_take_profits_tw`/`calc_position_size`、`execution.simulate_staged_exit`），確保各策略與 Champion 口徑一致、可直接比較。

## 1. 七個策略（§三 3.1）
1. Trend Following 趨勢追蹤（離現況最近，第一個樣板）
2. Breakout Retest 突破回測
3. Mean Reversion 均值回歸（需震盪 regime 判定）
4. Momentum 動能
5. Short Trend Continuation 空方延續（需券源/借券；等 Q01）
6. Failed Rally 反彈失敗（需券源；等 Q01）
7. Event-driven 事件驅動（需 point-in-time 公告可得日；D01）

每策略須有獨立：進場/出場/停損停利/部位/方向/成本/regime 適用/樣本外績效。

## 2. 架構
- `strategies/base.py`：`Strategy` 介面（`name`/`direction`/`regime_applicability`/`generate`）＋ `validate_plan` 契約。
- `strategies/registry.py`：註冊表（`register`/`all_strategies`/`names`）。
- `shadow_signals.strategy_name`：標記假想訊號屬哪個策略（Champion 候選＝`champion`）。
- 掃描端（階段 2 起）平行跑 `all_strategies().generate()`，出手者落 shadow（`kind='strategy_candidate'`），**不發 Telegram、不進風控**。
- 評估：接既有 walk-forward（無洩漏）＋ `factor_ablation` ＋ 每日 ICIR，以 `champion_r`（分批淨 R）為基準，分「策略×方向×regime」格。

## 3. 階段與閘門
1. **骨架**（本 PR）：介面＋註冊表＋`strategy_name` 欄位＋測試。不含策略、不接掃描。
2. **Trend Following ＋ 接掃描平行記錄**（shadow）。
3. **per-策略 OOS 報表**。
4. **Regime 分類器 PIT ＋ regime×策略 shadow 績效**（升級 `regime.py` 為逐筆標註）。
5. **（數月後、樣本足）Regime 路由接線** — risk:champion，使用者核准。

## 4. 影響層面
- 階段 1–4：新增程式模組＋shadow 欄位（加法式 migration）；盤後掃描多一批策略計算（**效能護欄**：限檔數/逾時跳過/背景化，Render 1 worker/12 threads）；新增研究頁。線上訊號/Champion/config_hash **不變**。
- 階段 5：改線上訊號、config_hash 變、需核准；可逆（champion snapshot 回滾＋revert）。

## 5. 上線判準（階段 5 前每策略必備）
足量成熟樣本、跨 ≥2–3 獨立行情窗口 OOS、ablation/edge 證據、成本後淨 R、換手、回撤；空方另須券源可得性。審查叮囑「不要急著把模型接到正式訊號」。
