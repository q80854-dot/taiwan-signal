"""單檔回測與正式流程對齊（2026-11-12；Q-P0-03 的最後一塊）。

原本 backtest_symbol_tw／backtest_symbol_tw_partial／walk_forward_backtest_tw：
  · 用訊號隔日『收盤』同時當計畫價與成交價（正式：訊號日收盤定錨、隔日『開盤』成交）；
  · 進場當日的盤中行情完全不檢查（當天就跌破停損也看不到）；
  · 沒有到期窗，會一直抱到停利或停損（正式：15 個日曆天到期）——等於無限持有而高估勝率。
"""
import datetime as dt

import backtester as bt
import execution


def _dates(n):
    base = dt.date(2026, 1, 1)
    return [(base + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _flat(n=200):
    return {"dates": _dates(n), "closes": [100.0] * n, "highs": [100.5] * n, "lows": [99.5] * n,
            "opens": [100.0] * n, "volumes": [10000] * n}


def _setup(monkeypatch, data, direction="buy", shares=3000):
    import stock_universe, data_fetcher, signal_engine
    # 這些測試驗的是回測的「到期機制」本身，固定用 15 天（資料長度依此設計），與現行設定值（60 天）無關
    import config as _cfg
    monkeypatch.setitem(_cfg.CIRCUIT_BREAKER, "signal_expire_days", 15)
    monkeypatch.setattr(stock_universe, "get_stock_info", lambda t: {"size_cat": "中型股", "volume_lots": 999999})
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": data if tf == "daily" else None)

    def _mtf(tf, **k):
        if len(tf["daily"]["closes"]) == 150:          # 訊號日＝index 149，隔日 index 150 進場
            return {"direction": direction, "score": 90, "adx_value": 30, "vol_ratio": 2.0, "weekly_bias": "bullish",
                    "entry_indicators": {"atr": {"value": 2.0}}}
        return {"direction": "none", "score": 0}
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw", _mtf)
    sgn = 1 if direction == "buy" else -1
    if direction == "sell":      # 放空已停發正式訊號；這裡測的是回測的放空機制（借券費、到期），故暫時開啟
        import config
        monkeypatch.setattr(config, "ENABLE_SHORT_SIGNALS", True)
    monkeypatch.setattr(signal_engine, "calc_stop_loss_tw", lambda d, p, a, ind, sc, lo, hi: round(p * (1 - 0.05 * sgn), 2))
    monkeypatch.setattr(signal_engine, "calc_take_profits_tw",
                        lambda d, p, sl, sc: {"tp1": round(p * (1 + 0.05 * sgn), 2), "tp2": round(p * (1 + 0.10 * sgn), 2),
                                              "tp3": round(p * (1 + 0.15 * sgn), 2), "rr1": 2.0})
    monkeypatch.setattr(signal_engine, "calc_position_size",
                        lambda p, sl, balance=None, size_cat=None, avg_volume_lots=None, code=None: {"shares": shares})


def test_symbol_backtest_fills_at_next_open_with_plan_anchored_on_signal_close(monkeypatch):
    d = _flat()
    d["closes"][149] = 100.0                              # 訊號日收盤＝計畫定錨
    d["opens"][150] = 101.0; d["closes"][150] = 103.0; d["highs"][150] = 103.5; d["lows"][150] = 100.5
    for i in range(151, 200):
        d["opens"][i] = d["closes"][i] = 103.0; d["highs"][i] = 103.5; d["lows"][i] = 102.5
    _setup(monkeypatch, d)
    out = bt.backtest_symbol_tw("A.TW", use_short_gates=False)
    t = out["trades"][0]
    assert t["entry"] == 101.0                            # 隔日開盤成交（原本是隔日收盤 103）
    assert t["sl"] == 95.0 and t["tp1"] == 105.0          # 停損停利以訊號日收盤 100 定錨（原本以 103 重算）
    assert out["entry_model"].startswith("訊號日收盤定錨")


def test_symbol_backtest_expires_instead_of_holding_forever(monkeypatch):
    """價格橫盤、既不停利也不停損：15 個日曆天後以收盤到期出場，不再抱到資料結束。"""
    _setup(monkeypatch, _flat())
    out = bt.backtest_symbol_tw("A.TW", use_short_gates=False)
    t = out["trades"][0]
    assert t["result"] == "expired" and out["expire_days"] == 15
    assert t["bar_out"] - 149 == 15                       # 訊號日起算第 15 個日曆天
    assert out["n_losses"] == 1 and out["n_wins"] == 0    # 到期小虧（成本）依淨損益歸類，不從分母消失


def test_symbol_backtest_checks_stop_on_entry_day(monkeypatch):
    """進場當日就跌破停損：當天即出場（原本要到隔天才開始檢查）。"""
    d = _flat()
    d["lows"][150] = 94.0; d["closes"][150] = 94.5
    _setup(monkeypatch, d)
    t = bt.backtest_symbol_tw("A.TW", use_short_gates=False)["trades"][0]
    assert t["result"] == "sl" and t["bar_in"] == 150 and t["bar_out"] == 150 and t["close"] == 95.0


def test_symbol_backtest_skips_entry_when_open_gaps_through_plan(monkeypatch):
    """開盤已越過 TP1（沒有報酬空間）或已穿越停損（風險無定義）：計畫失效，不進場。"""
    for gap_open in (106.0, 94.0):
        d = _flat(); d["opens"][150] = gap_open
        _setup(monkeypatch, d)
        assert bt.backtest_symbol_tw("A.TW", use_short_gates=False)["n_trades"] == 0


def test_partial_backtest_matches_staged_simulator(monkeypatch):
    """分批回測改用與正式推播同一個狀態機：逐腿與總損益和 simulate_staged_exit 完全一致。"""
    d = _flat()
    d["highs"][152] = 105.5; d["closes"][152] = 105.0
    d["highs"][155] = 110.5; d["closes"][155] = 110.0
    d["highs"][158] = 115.5; d["closes"][158] = 115.0
    for i in range(153, 200):
        d["lows"][i] = 104.0 if i < 156 else 108.0
        d["closes"][i] = max(d["closes"][i], d["lows"][i] + 0.5); d["highs"][i] = max(d["highs"][i], d["closes"][i])
        d["opens"][i] = d["closes"][i - 1]
    _setup(monkeypatch, d)
    out = bt.backtest_symbol_tw_partial("A.TW")
    t = out["trades"][0]
    bars = [{"bar_date": d["dates"][i], "open": d["opens"][i], "high": d["highs"][i], "low": d["lows"][i],
             "close": d["closes"][i]} for i in range(150, 200)]
    sim = execution.simulate_staged_exit(bars, entry=100.0, sl=95.0, tp1=105.0, tp2=110.0, tp3=115.0,
                                         direction="buy", shares=3000, gen_date=d["dates"][149],
                                         pnl_fn=bt.calc_tw_pnl, expire_days=15)
    assert t["result"] == "tp1+tp2+tp3" and t["n_legs_hit"] == 3
    assert t["pnl_twd"] == sim["pnl"] and [l["shares"] for l in t["legs"]] == [1000, 1000, 1000]
    assert out["exit_model"] == "staged" and out["final_balance"] - out["initial_balance"] == sim["pnl"]


def test_partial_backtest_expires_and_charges_short_borrow(monkeypatch):
    _setup(monkeypatch, _flat(), direction="sell")
    t = bt.backtest_symbol_tw_partial("A.TW")["trades"][0]
    assert t["result"] == "expired" and t["n_legs_hit"] == 0
    assert t["pnl_twd"] < bt.calc_tw_pnl(100.0, 100.0, "sell", 3000, days_held=0)     # 借券費有計入


def test_walk_forward_entry_day_stop_and_expiry(monkeypatch):
    d = _flat(); d["lows"][150] = 94.0
    _setup(monkeypatch, d)
    out = bt.walk_forward_backtest_tw("A.TW", train_bars=150, test_bars=30)
    w1 = out["window_results"][0]
    assert w1["n_trades"] == 1 and w1["win_rate"] == 0.0          # 進場當日停損
    _setup(monkeypatch, _flat())
    out2 = bt.walk_forward_backtest_tw("A.TW", train_bars=150, test_bars=30)
    assert out2["window_results"][0]["n_trades"] == 1             # 橫盤 → 15 天到期出場（不是抱到視窗結束）
