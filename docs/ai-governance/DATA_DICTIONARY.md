# Taiwan Signal｜資料字典（DATA_DICTIONARY）

> 治理 TS-DATA-001 / Phase 2。定義系統內「單位與欄位語意」的單一事實來源，消除
> shares/lots/現金/價格/% 的混淆。不變量由 `tests/test_unit_contracts.py` 硬性鎖住——
> 破壞任一關係 CI 即紅。相關：`SYSTEM_BASELINE.md`、`docs/UNIT_AUDIT_2026-11-06.md`。

最後更新：2026-11-08

---

## 1. 單位總表

| 概念 | 單位 | 定義 | 備註 |
|---|---|---|---|
| `shares` / `suggested_shares` | **股** | 零股整數股數 | **唯一權威的「數量」欄位**，一律以此為準 |
| `lots` | **張**（= 1000 股） | `shares / SHARES_PER_LOT` | 純顯示換算；`SHARES_PER_LOT=1000` |
| `price` / `entry_price` / `stop_loss` / `tp*` | **TWD/股** | 每股價格 | — |
| `risk_twd` | **TWD** | `shares × |entry − stop_loss|` | 單筆最大風險金額 |
| `position_value` | **TWD** | `shares × entry_price` | 部位市值（進場成本）|
| `risk_pct` | **%** | `risk_twd / 帳戶資金 × 100` | 受 `MAX_RISK_PER_TRADE`(2%) 上限約束 |
| `margin_pct` | **%** | `position_value / 帳戶資金 × 100` | 資金使用率 |
| `roundtrip_cost` | **TWD** | 來回手續費＋賣出證交稅（＋放空借券）| `calc_trade_cost` |
| `net_buy_twd`（外資） | **TWD** | 全市場外資單日淨買賣超金額 | 熔斷門檻以「億」為級距（1 億 = 1e8）|
| `foreign_net` / `trust_net`（個股） | **股** | 個股三大法人單日買賣超股數 | 顯示時 ÷1000 轉「張」|

## 2. ⚠️ 歷史命名陷阱（務必知道）

**DB `signals.suggested_lots` 欄位名叫「張」，但歷史遺留實際存「股數」。**

- 記憶體訊號 dict：`suggested_lots`＝**張**、另有 `suggested_shares`＝股。
- DB 列（原始）：`suggested_lots`＝**股**。
- **治本（TS-DATA-001，PR #124）**：`state_store` 的三個 DB 讀取邊界
  （`get_recent_signals`/`get_closed_trades`/`get_pending_signals`）一律經
  `with_share_units()` 補上明確的 `suggested_shares`（股）。
- **規則：任何讀取端要「數量」一律讀 `suggested_shares`（股），需要張就 ÷1000。
  不要再去解讀 `suggested_lots` 這個歷史欄位名。**

## 3. 部位計算不變量（`calc_position_size`，契約測試鎖定）

對任一非零部位，下列關係恆成立（見 `tests/test_unit_contracts.py`）：

1. `lots == round(shares / 1000, 3)`
2. `risk_twd == round(shares × |entry − stop_loss|, 0)`
3. `position_value == round(shares × entry_price, 0)`
4. `risk_pct == round(risk_twd / 帳戶資金 × 100, 2)`
5. `margin_pct == round(position_value / 帳戶資金 × 100, 1)`
6. **風控保證**：`risk_twd ≤ 帳戶資金 × MAX_RISK_PER_TRADE`（＋不足一股緩衝）——
   高價股也不例外（這正是 2026-08 改用零股為單位的原因）。
7. **部位上限**：`position_value ≤ 帳戶資金 ÷ MAX_SIMULTANEOUS_POSITIONS`（＋不足一股緩衝）。
8. **流動性上限**：有提供當日量時，`shares ≤ 當日成交股數 × 10%`。
9. **Fail-safe**：停損距離 ≤ 0 或價格 ≤ 0 → 不進場，所有金額欄位為 0。

## 4. 金額級距慣例

- 程式內金額一律 **TWD**；設定檔中大額門檻用科學記號標「億」：`1 億 = 1e8`，
  例：外資賣超暫停門檻 `foreign_sell_stop = -1800e8`（−1,800 億）。
- 顯示給使用者時以「億」為單位（`abs(nb)/1e8`）。

## 5. 未落地（待後續，Phase 2 剩餘）

- **Point-in-time 欄位**（`available_time`/`event_time`/`data_version`/`quality_status` 等）
  尚未全面落地；目前訊號 provenance 已有 `config_hash`/`strategy_version`（TS-GOV-001）。
- 全球市場的多市場單位適配（`SHARES_PER_LOT`、幣別、合約乘數）尚未抽象化（Phase 6）。
