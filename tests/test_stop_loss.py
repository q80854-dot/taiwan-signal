import pytest
from signal_engine import calc_stop_loss_tw


@pytest.mark.parametrize("size", ["大型股", "中型股", "小型股"])
@pytest.mark.parametrize("price,atr", [(27.45, 1.4), (238.5, 6.0), (1000.0, 15.0)])
def test_buy_stop_below_price_and_at_least_1_5_atr(size, price, atr):
    sl = calc_stop_loss_tw("buy", price, atr, {}, size_cat=size)
    assert sl < price
    assert price - sl >= max(atr * 1.5, price * 0.015) - 0.01


@pytest.mark.parametrize("size", ["大型股", "中型股", "小型股"])
def test_sell_stop_above_price(size):
    sl = calc_stop_loss_tw("sell", 100.0, 2.0, {}, size_cat=size)
    assert sl > 100.0 and sl - 100.0 >= 3.0 - 0.01


@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_stop_never_exceeds_8pct_even_for_high_atr(direction):
    """2026-10-09 修正：極高波動股（1.5×ATR > 股價 8%）時，停損距離仍不得突破 8% 風險上限。"""
    price, atr = 100.0, 10.0          # 1.5×ATR = 15 > 8；修正前會被放寬到 15%
    sl = calc_stop_loss_tw(direction, price, atr, {}, size_cat="中型股")
    dist = abs(price - sl)
    assert dist <= price * 0.08 + 0.01, f"停損距離 {dist} 超過 8% 上限"
    assert dist >= price * 0.015 - 0.01


def test_nearby_support_cannot_tighten_stop_below_1_5_atr():
    """2026-10-05 修正的回歸測試：支撐離現價太近時，停損仍要保有至少 1.5×ATR。"""
    ind = {"support_resistance": {"nearest_support": 26.9}}
    sl = calc_stop_loss_tw("buy", 27.45, 1.4, ind, size_cat="中型股")
    assert 27.45 - sl >= 1.4 * 1.5 - 0.01
