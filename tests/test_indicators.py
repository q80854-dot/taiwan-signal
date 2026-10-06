import indicators as I

UP = [100 + i for i in range(60)]
DOWN = [160 - i for i in range(60)]


def test_sma_of_window():
    assert I.sma([1, 2, 3, 4, 5], 5) == 3


def test_rsi_extremes():
    assert I.calc_rsi(UP)["value"] == 100.0       # 一路上漲 → 超買
    assert I.calc_rsi(DOWN)["value"] == 0.0       # 一路下跌 → 超賣


def test_atr_positive_and_matches_range():
    highs = [c + 1 for c in UP]
    lows = [c - 1 for c in UP]
    r = I.calc_atr(highs, lows, UP)
    assert r["valid"] and r["value"] > 0


def test_volume_ratio_flat_volume_is_one():
    assert I.calc_volume_ratio([1000] * 30)["ratio"] == 1.0


def test_adx_strong_trend():
    highs = [c + 1 for c in UP]
    lows = [c - 1 for c in UP]
    r = I.calc_adx(highs, lows, UP)
    assert r["valid"] and r["strong"] and r["bias"] == "bullish"
