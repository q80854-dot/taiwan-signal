"""組合級回測（2026-11-04）：單一共享帳戶、同時持倉上限、逐日模擬的邏輯驗證。
把評分/停損/部位函式 stub 成確定性，專注測試組合模擬本身（上限、出場、權益曲線）。"""
import datetime as dt

import backtester as bt


def _dates(n):
    base = dt.date(2026, 1, 1)
    return [(base + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _ohlcv(n=160):
    dates = _dates(n)
    closes = [100.0] * 151 + [112.0] * (n - 151)
    highs = [100.5] * 151 + [112.0] * (n - 151)
    lows = [99.0] * n
    opens = [100.0] * n
    vols = [10000] * n
    return {"dates": dates, "closes": closes, "highs": highs, "lows": lows, "opens": opens, "volumes": vols}


def test_portfolio_backtest_shared_account_and_cap(monkeypatch):
    import stock_universe, data_fetcher, signal_engine
    monkeypatch.setattr(bt, "_tw50_targets", lambda *a, **k: ["A.TW", "B.TW"])
    monkeypatch.setattr(stock_universe, "get_stock_info", lambda t: {"size_cat": "中型股", "volume_lots": 999999})
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": _ohlcv())

    def _mtf(tf, **k):
        wl = len(tf["daily"]["closes"])
        if wl == 150:   # 只在第 150 根（index 150 的那天）給買訊
            return {"direction": "buy", "score": 90, "adx_value": 30, "vol_ratio": 2.0,
                    "weekly_bias": "bullish", "adx_bias": "bullish",
                    "entry_indicators": {"atr": {"value": 2.0}}}
        return {"direction": "none", "score": 0}
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw", _mtf)
    monkeypatch.setattr(signal_engine, "calc_stop_loss_tw", lambda d, p, a, ind, sc, lo, hi: round(p * 0.95, 2))
    monkeypatch.setattr(signal_engine, "calc_take_profits_tw", lambda d, p, sl, sc: {"tp1": round(p * 1.10, 2), "tp2": p * 1.15, "tp3": p * 1.25, "rr1": 2.0})
    monkeypatch.setattr(signal_engine, "calc_position_size",
                        lambda p, sl, balance=None, size_cat=None, avg_volume_lots=None: {"shares": 1000})

    out = bt.portfolio_backtest_tw(min_score=65, max_positions=1)
    assert out["method"] == "portfolio" and out["n_tickers"] == 2
    assert out["max_concurrent"] == 1                 # 同時持倉上限=1 被遵守
    assert out["n_trades"] == 1 and out["n_wins"] == 1   # 只開 1 檔（cap），觸 tp1 獲利平倉
    assert out["by_direction"]["buy"]["n_trades"] == 1
    assert len(out["equity_curve"]) == len(out["dates"]) and out["dates"]           # 有逐日權益曲線
    assert out["final_balance"] > out["initial_balance"]                           # 這筆是賺的


def test_portfolio_backtest_no_data(monkeypatch):
    import stock_universe, data_fetcher
    monkeypatch.setattr(bt, "_tw50_targets", lambda *a, **k: ["A.TW"])
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": {"closes": []})
    out = bt.portfolio_backtest_tw()
    assert "error" in out
