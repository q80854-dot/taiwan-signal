"""策略執行＋記錄（階段 2b-1）。

對單一標的跑所有已註冊策略的 generate()，驗證後記錄到 strategy_signals。全程防禦性：
單一策略丟例外不影響其他策略；呼叫端（階段 2b-2 的 scanner hook）另包一層 try/except，
確保任何策略錯誤都不會中斷真實掃描。純 shadow：不發訊號、不進風控、不影響線上訊號。
"""
import logging

logger = logging.getLogger(__name__)


def run_and_record(bar_date, ticker, stock_info, tf_data, market_overview, inst_data=None) -> int:
    """跑所有內建策略、記錄出手者到 strategy_signals。回傳實際新增筆數。"""
    from . import load_builtin, registry
    from .base import validate_plan
    from . import store as sstore

    load_builtin()
    plans = []
    for s in registry.all_strategies():
        try:
            plan = s.generate(ticker, stock_info, tf_data, market_overview, inst_data)
        except Exception as e:
            logger.debug(f"strategy {getattr(s, 'name', '?')} generate({ticker}): {e}")
            plan = None
        if plan and validate_plan(plan):
            plans.append((s.name, plan))
    if not plans:
        return 0
    try:
        return sstore.record_candidates(bar_date, ticker, stock_info, plans)
    except Exception as e:
        logger.warning(f"strategy record_candidates({ticker}): {e}")
        return 0
