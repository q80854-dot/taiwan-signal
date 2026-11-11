"""策略共用介面（Phase 1 骨架）。

每個獨立策略子類 Strategy，實作：
  - name / direction：識別與方向（"long" | "short" | "both"）。
  - regime_applicability(regime)：回傳 0..1，此策略在該市場狀態下的啟用權重（預設恆 1）。
  - generate(...)：回傳一個「訊號計畫」dict 或 None（不出手）。

契約（REQUIRED_PLAN_KEYS）：generate 回傳的 plan 必含 direction/entry/stop/tp1/tp2/tp3/score/reason。
出場價位（stop/tp）與部位大小在 Phase 2 起一律走既有共用函式（signal_engine.calc_* /
execution.simulate_staged_exit），確保各策略與 Champion 口徑一致——骨架階段僅定義契約與驗證。
"""
from typing import Optional

REQUIRED_PLAN_KEYS = ("direction", "entry", "stop", "tp1", "tp2", "tp3", "score", "reason")
VALID_DIRECTIONS = ("long", "short", "both")


class Strategy:
    name: str = "base"
    direction: str = "both"          # "long" | "short" | "both"

    def regime_applicability(self, regime: dict) -> float:
        """此策略在給定 regime 下的啟用權重（0=不啟用, 1=完全啟用）。預設恆啟用。"""
        return 1.0

    def generate(self, ticker, stock_info, tf_data, market_overview, inst_data=None) -> Optional[dict]:
        """回傳訊號計畫 dict（含 REQUIRED_PLAN_KEYS）或 None（不出手）。子類覆寫。"""
        raise NotImplementedError


def validate_plan(plan) -> bool:
    """驗證策略回傳的計畫是否符合契約（欄位齊備、方向合法、價位為數值）。"""
    if not isinstance(plan, dict):
        return False
    if any(k not in plan for k in REQUIRED_PLAN_KEYS):
        return False
    if plan.get("direction") not in ("long", "short", "buy", "sell"):
        return False
    for k in ("entry", "stop", "tp1", "tp2", "tp3", "score"):
        v = plan.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
    return True
