"""Q-P0-01／02／03／04：組合級回測與正式流程對齊（2026-11-12）。

原本 portfolio_backtest_tw：出場只在 TP1 一次全出（正式是 TP1/TP2/TP3 各 1/3）；進場用訊號隔日
『收盤』並以該收盤重算停損停利（正式是訊號日收盤定錨、隔日『開盤』成交）；所有回測呼叫
calc_tw_pnl 都沒傳持有天數，放空借券費恆為 0。
"""
import datetime as dt
import re

import backtester as bt
import execution


def _dates(n):
    base = dt.date(2026, 1, 1)
    return [(base + dt.timedelta(days=i)).isoformat() for i in range(n)]


def _flat(n=175):
    return {"dates": _dates(n), "closes": [100.0] * n, "highs": [100.5] * n, "lows": [99.5] * n,
            "opens": [100.0] * n, "volumes": [10000] * n}


def _setup(monkeypatch, data, direction="buy", shares=3000):
    import stock_universe, data_fetcher, signal_engine
    # 這些測試驗的是回測的「到期機制」本身，固定用 15 天（資料長度依此設計），與現行設定值（60 天）無關
    import config as _cfg
    monkeypatch.setitem(_cfg.CIRCUIT_BREAKER, "signal_expire_days", 15)
    monkeypatch.setattr(bt, "_tw50_targets", lambda *a, **k: ["A.TW"])     # 自訂清單（不帶 PIT 標記）
    monkeypatch.setattr(stock_universe, "get_stock_info", lambda t: {"size_cat": "中型股", "volume_lots": 999999})
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": data)

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
                        lambda p, sl, balance=None, size_cat=None, avg_volume_lots=None: {"shares": shares})


def _bars(data, i0, i1):
    return [{"bar_date": data["dates"][i], "open": data["opens"][i], "high": data["highs"][i],
             "low": data["lows"][i], "close": data["closes"][i]} for i in range(i0, i1)]


def test_backtester_short_borrow_days():
    """放空淨損益隨持有天數下降（借券費）；做多不受影響。"""
    s1, s7, s15 = (bt.calc_tw_pnl(100, 95, "sell", 1000, days_held=d) for d in (1, 7, 15))
    assert s1 > s7 > s15
    assert bt.calc_tw_pnl(100, 105, "buy", 1000, days_held=1) == bt.calc_tw_pnl(100, 105, "buy", 1000, days_held=15)
    assert bt._held_days(["2026-01-02", "2026-01-05", "2026-01-12"], 0, 2) == 10     # 日曆天，不是 K 棒數
    assert bt._held_days([], 0, 5) == 7                                              # 無日期：交易日×7/5 近似


def test_no_backtest_pnl_call_omits_days_held():
    """所有回測路徑呼叫 calc_tw_pnl 都必須傳持有天數（原本 9 處全漏，放空借券費恆為 0）。
    分批回測改用 StagedPosition 後直接呼叫處減少（狀態機內部一律帶 days_held）。"""
    src = open(bt.__file__, encoding="utf-8").read()
    calls = [m.group(0) for m in re.finditer(r"(?<!def )calc_tw_pnl\((?:[^()]|\([^()]*\))*\)", src)]
    assert len(calls) >= 6
    assert all("days_held" in c for c in calls), [c for c in calls if "days_held" not in c]


def test_portfolio_next_executable_bar(monkeypatch):
    """訊號日（index 149）收盤定錨計畫價位，隔日（index 150）『開盤』成交，不是隔日收盤。"""
    d = _flat()
    d["opens"][150] = 101.0; d["closes"][150] = 103.0; d["highs"][150] = 103.5; d["lows"][150] = 100.5
    _setup(monkeypatch, d)
    out = bt.portfolio_backtest_tw(min_score=65, max_positions=1)
    t = out["trades"][0]
    assert t["entry"] == 101.0                    # 開盤成交（原本會是收盤 103）
    assert t["bar_in"] == d["dates"][149]         # 訊號日
    assert out["entry_model"].startswith("訊號日收盤定錨")


def test_portfolio_exit_parity_with_staged_simulator(monkeypatch):
    """同一段 K 棒：組合回測的每一腿、總損益與 execution.simulate_staged_exit 完全一致（差異為 0）。"""
    d = _flat()
    d["highs"][152] = 105.5; d["closes"][152] = 105.0      # TP1（105）
    d["highs"][155] = 110.5; d["closes"][155] = 110.0      # TP2（110）
    d["highs"][158] = 115.5; d["closes"][158] = 115.0      # TP3（115）
    for i in range(153, 175):                              # 價格一路墊高，不觸移動停損（TP1→成本、TP2→TP1）
        d["lows"][i] = 104.0 if i < 156 else 108.0
        d["closes"][i] = max(d["closes"][i], d["lows"][i] + 0.5); d["highs"][i] = max(d["highs"][i], d["closes"][i])
    _setup(monkeypatch, d)
    out = bt.portfolio_backtest_tw(min_score=65, max_positions=1)
    t = out["trades"][0]
    sim = execution.simulate_staged_exit(_bars(d, 150, 175), entry=100.0, sl=95.0, tp1=105.0, tp2=110.0, tp3=115.0,
                                         direction="buy", shares=3000, gen_date=d["dates"][149],
                                         pnl_fn=bt.calc_tw_pnl, expire_days=15)
    assert [l["stage"] for l in t["legs"]] == ["tp1", "tp2", "tp3"] == [l["stage"] for l in sim["legs"]]
    assert t["pnl_twd"] == sim["pnl"] and t["result"] == sim["result"] == "tp3"
    assert out["exit_model"] == "staged" and out["final_balance"] - out["initial_balance"] == sim["pnl"]


def test_portfolio_tp1_then_breakeven_stop_not_counted_as_full_tp1(monkeypatch):
    """TP1 後回落觸保本停損：只有 1/3 在 TP1 出場，其餘 2/3 保本——原本會把整筆算成 TP1 全出。"""
    d = _flat()
    d["highs"][152] = 105.5; d["closes"][152] = 104.0
    d["lows"][153] = 99.0; d["closes"][153] = 99.5         # 跌破成本 100
    _setup(monkeypatch, d)
    t = bt.portfolio_backtest_tw(min_score=65, max_positions=1)["trades"][0]
    assert [(l["stage"], l["shares"]) for l in t["legs"]] == [("tp1", 1000), ("stop", 2000)]
    full_tp1 = bt.calc_tw_pnl(100.0, 105.0, "buy", 3000)
    assert 0 < t["pnl_twd"] < full_tp1 / 2                 # 遠低於「TP1 全出」的舊算法


def test_portfolio_short_charges_borrow_fee_by_days(monkeypatch):
    """放空持有到期：損益含借券費（以實際持有日曆天數計），與無借券費版本有差。"""
    d = _flat()
    _setup(monkeypatch, d, direction="sell")
    t = bt.portfolio_backtest_tw(min_score=65, max_positions=1)["trades"][0]
    assert t["result"] == "expired" and t["hold_days"] >= 15
    no_borrow = bt.calc_tw_pnl(100.0, 100.0, "sell", 3000, days_held=0)
    assert t["pnl_twd"] < no_borrow                        # 借券費有被計入
    assert t["pnl_twd"] == bt.calc_tw_pnl(100.0, 100.0, "sell", 3000, days_held=t["hold_days"])


def test_portfolio_discloses_universe_is_not_point_in_time(monkeypatch):
    """Q-P0-04：標的池取自現在的成分股（無歷史 PIT 資料），必須明示，不得當成正式有效歷史績效。"""
    _setup(monkeypatch, _flat())
    out = bt.portfolio_backtest_tw(min_score=65, max_positions=1)
    assert out["universe_pit"].startswith("PIT_UNAVAILABLE")       # 自訂清單：未做歷史成分檢查
    assert any("倖存者" in x for x in out["limitations"])
