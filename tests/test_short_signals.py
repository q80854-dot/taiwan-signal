"""做空訊號（2026-10-16）：使用者要求策略也要會做空、實際發出的訊號要有做空，
策略學習也要涵蓋做空。這裡守住三件事：
  1) config 放空門檻已依指示放寬、且放空被設為可執行（SHORT_EXECUTABLE）。
  2) Telegram 推播：可執行放空掛「放空風險提醒」而非「研究版/不可執行」橫幅；
     若放空被關成不可執行才回到研究版橫幅；做多完全不掛放空相關橫幅。
  3) 策略學習有「方向」維度，能把做多 vs 做空分開統計（讓做空被真的學到、看得到）。
"""
import config
import telegram_bot as tb
import learning


def _base_sell_sig(executable):
    return {
        "name": "測試股", "code": "9999", "direction": "sell",
        "current_price": 100.0, "stop_loss": 105.0, "tp1": 92.0,
        "score": 72, "weekly_bias": "bearish", "grade": "B",
        "executable": executable,
    }


def test_config_short_relaxed_and_executable():
    assert config.ENABLE_SHORT_SIGNALS is True
    assert config.SHORT_EXECUTABLE is True
    # 放空門檻仍高於做多，但已依指示小幅放寬
    assert config.SHORT_SIGNAL_THRESH["min_score"] == 70
    assert config.SHORT_SIGNAL_THRESH["min_adx"] == 22
    assert config.SHORT_SIGNAL_THRESH["min_vol_ratio"] == 1.3
    # 仍高於做多門檻（避免做空變得跟做多一樣寬鬆）
    assert config.SHORT_SIGNAL_THRESH["min_score"] > config.SIGNAL_THRESHOLDS["min_score"]
    assert config.SHORT_MAX_SENTIMENT == 65
    assert config.SHORT_REQUIRE_WEEKLY_BEARISH is True


def test_telegram_executable_short_shows_risk_not_research_banner():
    msg = tb.format_signal_paid(_base_sell_sig(executable=True))
    assert "放空訊號" in msg and "做空特有風險" in msg
    assert "研究版" not in msg          # 可執行放空不應再被標成研究版
    assert "📉 做空" in msg


def test_telegram_nonexecutable_short_shows_research_banner():
    msg = tb.format_signal_paid(_base_sell_sig(executable=False))
    assert "研究版" in msg


def test_telegram_long_has_no_short_banner():
    sig = _base_sell_sig(executable=True)
    sig.update(direction="buy", stop_loss=95.0, tp1=108.0, weekly_bias="bullish")
    msg = tb.format_signal_paid(sig)
    assert "研究版" not in msg
    assert "放空訊號" not in msg
    assert "📈 做多" in msg


def test_learning_has_direction_dimension():
    names = [name for name, _ in learning._dims()]
    assert "方向" in names
    fn = dict(learning._dims())["方向"]
    assert fn({"direction": "buy"}) == "做多"
    assert fn({"direction": "sell"}) == "做空"
