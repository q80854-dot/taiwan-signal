"""外資賣超熔斷三層（2026-11-08 使用者核准、依台股實際量能重校）。

全市場外資單日淨賣超門檻：warning −500億（降信心/扣8）、heavy −1,000億（扣12）、
extreme −1,800億（史上前幾大恐慌日，暫停新多單 stop_buy）。放空不受影響。
見 config.CIRCUIT_BREAKER 與 docs/ai-governance/STRATEGY_ARCHITECTURE.md §6。
"""
from risk_manager import check_foreign_flow


def test_routine_sell_no_longer_triggers():
    """日常等級賣超（−67億/−300億，2026 幾乎天天）不再觸發任何熔斷。"""
    assert check_foreign_flow({"foreign": {"net_buy_twd": -67e8}})["level"] == "normal"
    assert check_foreign_flow({"foreign": {"net_buy_twd": -300e8}})["level"] == "normal"


def test_caution_tier_reduces_confidence():
    """≥500億：warning，只降低信心度、不停單。"""
    r = check_foreign_flow({"foreign": {"net_buy_twd": -600e8}})
    assert r["level"] == "warning" and r["action"] == "reduce_confidence"


def test_heavy_tier_reduces_confidence():
    """≥1,000億（史上前10大等級）：heavy，大幅降低信心度、仍不停單。"""
    r = check_foreign_flow({"foreign": {"net_buy_twd": -1200e8}})
    assert r["level"] == "heavy" and r["action"] == "reduce_confidence"


def test_extreme_tier_halts_buy():
    """≥1,800億（史上前幾大恐慌日）：extreme，暫停新多單（stop_buy）。"""
    r = check_foreign_flow({"foreign": {"net_buy_twd": -2000e8}})
    assert r["level"] == "extreme" and r["action"] == "stop_buy"


def test_positive_and_normal_unchanged():
    assert check_foreign_flow({"foreign": {"net_buy_twd": 30e8}})["level"] == "positive"
    assert check_foreign_flow({"foreign": {"net_buy_twd": 0}})["level"] == "normal"
