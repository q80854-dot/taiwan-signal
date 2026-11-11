"""策略 1 Trend Following（階段 2a）。

以 monkeypatch 控制 check_multi_timeframe_tw / calc_all_indicators 的輸出，確定性驗證做多門檻與
各 gate；出場走真實共用函式（calc_stop_loss_tw/calc_take_profits_tw）。純 shadow、不接掃描。
"""
import signal_engine
import indicators
from strategies import base, registry
from strategies.trend_following import TrendFollowing


_DAILY = {"daily": {"closes": [100] * 10, "lows": [95, 96, 97, 96, 98]}}
_STOCK = {"size_cat": "中型股"}


def _patch(monkeypatch, *, direction="buy", adx=30, weekly="bullish", ema_bias="bullish", rsi=60):
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw",
                        lambda tf, **k: {"direction": direction, "adx_value": adx,
                                         "weekly_bias": weekly, "rsi_value": rsi, "score": 9})
    monkeypatch.setattr(indicators, "calc_all_indicators",
                        lambda d: {"valid": True, "ema": {"bias": ema_bias, "valid": True},
                                   "atr": {"value": 2.0}, "current_price": 100.0,
                                   "support_resistance": {}})


def test_generates_on_valid_uptrend(monkeypatch):
    _patch(monkeypatch)
    plan = TrendFollowing().generate("2330.TW", _STOCK, _DAILY, {})
    assert plan is not None
    assert base.validate_plan(plan) is True
    assert plan["direction"] == "long"
    assert plan["stop"] < plan["entry"] < plan["tp1"] < plan["tp2"] < plan["tp3"]


def test_skips_when_not_bullish_direction(monkeypatch):
    _patch(monkeypatch, direction="sell")
    assert TrendFollowing().generate("X", _STOCK, _DAILY, {}) is None


def test_skips_when_adx_weak(monkeypatch):
    _patch(monkeypatch, adx=15)
    assert TrendFollowing().generate("X", _STOCK, _DAILY, {}) is None


def test_skips_when_weekly_bearish(monkeypatch):
    _patch(monkeypatch, weekly="bearish")
    assert TrendFollowing().generate("X", _STOCK, _DAILY, {}) is None


def test_skips_when_not_bullish_alignment(monkeypatch):
    _patch(monkeypatch, ema_bias="neutral")
    assert TrendFollowing().generate("X", _STOCK, _DAILY, {}) is None


def test_skips_when_overheated(monkeypatch):
    _patch(monkeypatch, rsi=80)
    assert TrendFollowing().generate("X", _STOCK, _DAILY, {}) is None


def test_skips_when_no_daily():
    assert TrendFollowing().generate("X", _STOCK, {}, {}) is None


def test_load_builtin_registers():
    registry.clear()
    import importlib
    import strategies
    importlib.import_module("strategies").load_builtin()
    assert "trend_following" in registry.names()
    registry.clear()
