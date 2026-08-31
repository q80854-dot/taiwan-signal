# config.py — 修正 ema_trend 降低 K線需求
# 找到 INDICATOR_PARAMS 這段，把 ema_trend 從 120 改成 60

INDICATOR_PARAMS = {
    "ema_fast":   10,
    "ema_mid":    20,
    "ema_slow":   60,
    "ema_trend":  60,   # ★ 從 120 改成 60（需要 65 根，大多數股票都有）
    "rsi_period":      14,
    "rsi_overbought":  70,
    "rsi_oversold":    30,
    "rsi_bull_zone":   50,
    "rsi_bear_zone":   50,
    "macd_fast":   12,
    "macd_slow":   26,
    "macd_signal":  9,
    "bb_period": 20,
    "bb_std":     2,
    "atr_period": 14,
    "adx_period": 14,
    "adx_min":    20,
    "vol_period":     20,
    "vol_surge_ratio": 1.5,
    "support_lookback": 60,
    "fib_levels": [0.236, 0.382, 0.5, 0.618, 0.786],
    "max_hold_days": 20,
    "min_hold_days":  3,
}
IND = INDICATOR_PARAMS