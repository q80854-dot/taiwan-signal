from risk_manager import check_foreign_flow


def test_extreme_foreign_sell_no_longer_hard_stops():
    r = check_foreign_flow({"foreign": {"net_buy_twd": -67e8}})
    assert r["level"] == "extreme" and r["action"] == "reduce_confidence"


def test_foreign_thresholds_unchanged():
    assert check_foreign_flow({"foreign": {"net_buy_twd": -30e8}})["level"] == "warning"
    assert check_foreign_flow({"foreign": {"net_buy_twd": 30e8}})["level"] == "positive"
