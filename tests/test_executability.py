"""Q-P0-07 交易可執行性（2026-11-12）：只擋「確定無法成交」的情形，全部用既有資料與交易所規則判定。

1) 一價鎖死的漲跌停：做多遇鎖漲停買不到、放空遇鎖跌停空不到 → 不算成交；
   多單遇鎖跌停賣不掉、空單遇鎖漲停補不回 → 出場順延到可成交的 K 棒。
2) 放空須整張：零股不得融券、不得借券賣出。
3) 放空券源：非信用交易標的或融券額度用盡 → 不發放空訊號；資料沒取到不誤擋。
"""
from datetime import datetime, timedelta

import execution as ex
import shadow
import signal_engine as se
from scanner import TWScanEngine
from state_store import store

_PNL = lambda e, x, d, s, days_held=0: (x - e) * s if d == "buy" else (e - x) * s


def _b(day, o, h, l, c):
    return {"bar_date": day, "open": o, "high": h, "low": l, "close": c}


def test_limit_locked_detection_is_conservative():
    assert ex.limit_locked(_b("d", 110, 110, 110, 110), 100) == "up"         # 一價鎖漲停
    assert ex.limit_locked(_b("d", 90, 90, 90, 90), 100) == "down"           # 一價鎖跌停
    assert ex.limit_locked(_b("d", 110, 110, 108, 110), 100) is None         # 盤中打開過：仍可能成交，不擋
    assert ex.limit_locked(_b("d", 100, 100, 100, 100), 100) is None         # 無量一字線但沒到漲跌停
    assert ex.limit_locked(_b("d", 110, 110, 110, 110), None) is None        # 缺前收：不判定
    assert ex.entry_blocked_by_limit("buy", _b("d", 110, 110, 110, 110), 100)
    assert not ex.entry_blocked_by_limit("sell", _b("d", 110, 110, 110, 110), 100)   # 鎖漲停時放空賣得出
    assert ex.exit_blocked_by_limit("buy", _b("d", 90, 90, 90, 90), 100)             # 多單鎖跌停賣不掉
    assert ex.exit_blocked_by_limit("sell", _b("d", 110, 110, 110, 110), 100)        # 空單鎖漲停補不回


def test_staged_stop_is_deferred_past_locked_limit_down_bars():
    """多單連兩天一價鎖跌停：賣不掉，停損不得記在鎖死日；第三天打開才以開盤價（跳空）出場。"""
    bars = [_b("2026-10-02", 90, 90, 90, 90),            # 鎖跌停（前收 100）
            _b("2026-10-05", 81, 81, 81, 81),            # 再鎖跌停（前收 90）
            _b("2026-10-06", 78, 80, 76, 79)]            # 打開
    sim = ex.simulate_staged_exit(bars, entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
                                  shares=300, gen_date="2026-10-01", pnl_fn=_PNL, expire_days=15, prev_close=100)
    assert [l["stage"] for l in sim["legs"]] == ["stop"]
    assert sim["exit_date"] == "2026-10-06" and sim["legs"][0]["price"] == 78     # 不是 95，也不是 90
    naive = ex.simulate_staged_exit(bars, entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
                                    shares=300, gen_date="2026-10-01", pnl_fn=_PNL, expire_days=15)
    assert naive["exit_date"] == "2026-10-02"            # 不給前收＝第一根無從判定（舊行為），對照用
    assert sim["pnl"] < naive["pnl"]                     # 鎖死日假裝能賣會低估虧損


def test_single_exit_simulator_also_defers_on_locked_bars():
    bars = [_b("2026-10-02", 90, 90, 90, 90), _b("2026-10-05", 85, 86, 84, 85)]
    out = ex.simulate_exit(planned_entry=100, entry_fill=100, stop=95, direction="buy", bars_after=bars,
                           bar_date="2026-10-01", expire_days=15, risk=5, tp1=105, tp2=110, tp3=115, prev_close=100)
    assert out["result"] == "sl" and out["exit_date"] == "2026-10-05" and out["exit_price"] == 85
    assert out["sim_version"].startswith("exit-v3")


_TK = "EXECLIM.TW"
_NOW = datetime.now(shadow.TPE).replace(tzinfo=None)


def _d(days_ago):
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def test_shadow_marks_locked_entry_as_unfilled_not_a_trade():
    shadow.ensure_table()
    def cleanup():
        with store._conn() as conn:
            conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (_TK,))
            conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (_TK,))
    cleanup()
    try:
        with store._conn() as conn:
            conn.execute(
                "INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,"
                "status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_d(5), _TK, "9999", "測試", "candidate", "buy", 80, 100.0, 95.0, 112.0, 118.0, 125.0, "pending", _d(5)))
            for day, o, h, l, c in ((_d(5), 99, 101, 98, 100), (_d(4), 110, 110, 110, 110), (_d(3), 112, 121, 111, 120)):
                conn.execute("INSERT OR REPLACE INTO ohlcv_bars (ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", (_TK, "daily", day, o, h, l, c, 1000, day))
        res = shadow.resolve_pending()
        with store._conn() as conn:
            r = dict(conn.execute("SELECT status,result,r_multiple FROM shadow_signals WHERE ticker=?", (_TK,)).fetchone())
        assert (r["status"], r["result"]) == ("unresolved", "unfilled_limit") and r["r_multiple"] is None
        assert res["unfilled_limit"] == 1                # 買不到的漲停不得算成一筆賺錢的交易
    finally:
        cleanup()


def test_short_must_be_whole_lots():
    assert se.short_whole_lots(2556) == 2000 and se.short_whole_lots(999) == 0 and se.short_whole_lots(1000) == 1000
    pos = {"shares": 1800, "lots": 1.8, "risk_twd": 9000, "liquidity_capped": False}
    out = se.resize_position(pos, 50.0, 55.0, 1000)
    assert out["shares"] == 1000 and out["lots"] == 1.0 and out["risk_twd"] == 5000 and out["position_value"] == 50000
    assert "short_odd_lot" in shadow.REASONS


def test_fit_to_cash_keeps_shorts_in_whole_lots():
    base = {"direction": "sell", "entry_price": 50.0, "stop_loss": 55.0, "suggested_shares": 3000}
    out = se.fit_to_cash(dict(base), cash=120000)               # 現金只夠 2400 股 → 放空縮成 2 張
    assert out["suggested_shares"] == 2000
    assert se.fit_to_cash(dict(base), cash=40000) is None       # 不足 1 張 → 不發
    assert se.fit_to_cash(dict(base), cash=10 ** 9)["suggested_shares"] == 3000
    buy = se.fit_to_cash({"direction": "buy", "entry_price": 50.0, "stop_loss": 45.0, "suggested_shares": 3000}, cash=120000)
    assert buy["suggested_shares"] == 2400                      # 做多不受整張限制（零股可買）


def test_short_availability_only_blocks_when_data_confirms():
    m = {"2330": {"source": "twse_openapi", "short_utilization_pct": 35.0},
         "2317": {"source": "twse_openapi", "short_utilization_pct": 100.0}}
    f = TWScanEngine._short_availability
    assert f({"ticker": "2330.TW", "code": "2330"}, m) == "ok"
    assert f({"ticker": "2317.TW", "code": "2317"}, m) == "no_quota"
    assert f({"ticker": "9999.TW", "code": "9999"}, m) == "not_marginable"      # 上市資料有取到、名單內沒有
    assert f({"ticker": "6488.TWO", "code": "6488"}, m) == "unknown"             # 上櫃資料這次沒取到：不誤擋
    assert f({"ticker": "2330.TW", "code": "2330"}, {}) == "unknown"


def test_short_signal_below_one_lot_is_rejected_end_to_end(monkeypatch):
    """放空建議部位不足 1 張：現實中無法下單 → 不產生訊號，並以 short_odd_lot 記成假想單。"""
    import event_filter
    calls = []
    monkeypatch.setattr(shadow, "note_reject", lambda *a, **k: calls.append(a[7]))
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "control_from_engine", lambda *a, **k: None)
    monkeypatch.setattr(event_filter, "check", lambda code, within_days=None: {})
    monkeypatch.setattr(se, "check_multi_timeframe_tw",
                        lambda tf, disabled_factors=None: {"direction": "sell", "score": 90, "adx_value": 35,
                                                           "adx_bias": "bearish", "vol_ratio": 2.5,
                                                           "weekly_bias": "bearish", "rsi_value": 35,
                                                           "conditions_met": [], "conditions_fail": []})
    closes = [300 - i * 0.4 for i in range(260)]
    daily = {"closes": closes, "highs": [c + 1 for c in closes], "lows": [c - 1 for c in closes],
             "opens": [c + 0.2 for c in closes], "volumes": [1000 + (i % 5) * 50 for i in range(260)],
             "current_price": closes[-1]}
    mo = {"can_trade": True, "market_status": "normal", "sentiment_score": 30,
          "regime": {"available": True, "trend": "bearish"}}
    stock = {"name": "測試", "sector": "半導體業", "size_cat": "中型股", "code": "9999", "volume_lots": 99999}

    def size(shares):
        return lambda p, sl, **k: {"shares": shares, "lots": shares / 1000, "risk_twd": 1000, "risk_pct": 0.2,
                                   "position_value": shares * p, "margin_pct": 1, "roundtrip_cost": 100,
                                   "breakeven_pct": 0.1, "liquidity_capped": False}
    monkeypatch.setattr(se, "calc_position_size", size(510))
    assert se.generate_signal_tw("9999.TW", stock, {"daily": daily}, mo) is None
    assert calls == ["short_odd_lot"]
    monkeypatch.setattr(se, "calc_position_size", size(2556))
    sig = se.generate_signal_tw("9999.TW", stock, {"daily": daily}, mo)
    assert sig and sig["direction"] == "sell" and sig["suggested_shares"] == 2000 and sig["suggested_lots"] == 2.0
