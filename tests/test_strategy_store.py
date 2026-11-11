"""策略假想訊號儲存與結算（階段 2b-1）。

獨立表 strategy_signals（鍵 bar_date+ticker+strategy_name，避開 shadow UNIQUE 衝突）；
record 去重、resolve 以分批出場算 champion_r、overview 依策略分列、runner 跑策略並記錄。
純 shadow，不接掃描。
"""
import signal_engine
import indicators
from strategies import store as sstore
from strategies import runner

_TK = "STRAT_TEST.TW"
_PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0,
         "tp1": 105.0, "tp2": 110.0, "tp3": 115.0, "score": 80, "reason": "x"}


def _cleanup():
    sstore.ensure_table()
    from state_store import store
    with store._conn() as conn:
        conn.execute("DELETE FROM strategy_signals WHERE ticker=?", (_TK,))
        conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (_TK,))


def _insert_bars(rows):
    from state_store import store
    with store._conn() as conn:
        for d, o, h, l, c in rows:
            conn.execute("INSERT OR REPLACE INTO ohlcv_bars (ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (_TK, "daily", d, o, h, l, c, 1000, d))


def test_record_and_dedup():
    _cleanup()
    try:
        n1 = sstore.record_candidates("2026-10-01", _TK, {"code": "9999", "name": "測試"},
                                      [("trend_following", dict(_PLAN)), ("mean_reversion", dict(_PLAN))])
        assert n1 == 2
        # 同 (bar_date,ticker,strategy_name) 再記一次 → 去重，0 新增
        n2 = sstore.record_candidates("2026-10-01", _TK, {"code": "9999"},
                                      [("trend_following", dict(_PLAN))])
        assert n2 == 0
    finally:
        _cleanup()


def test_resolve_computes_champion_r():
    _cleanup()
    try:
        sstore.record_candidates("2026-10-01", _TK, {"code": "9999", "name": "測試"},
                                 [("trend_following", dict(_PLAN))])
        _insert_bars([("2026-10-02", 100, 106, 99, 105),    # 觸 tp1
                      ("2026-10-03", 105, 111, 104, 110),   # 觸 tp2
                      ("2026-10-04", 110, 116, 109, 115)])  # 觸 tp3 → 全出場
        out = sstore.resolve_pending()
        assert out["closed"] == 1
        from state_store import store
        with store._conn() as conn:
            row = dict(conn.execute("SELECT * FROM strategy_signals WHERE ticker=?", (_TK,)).fetchone())
        assert row["status"] == "closed"
        assert row["champion_r"] is not None and row["champion_r"] > 0   # 三段皆獲利
        ov = sstore.overview()
        tf = [s for s in ov["strategies"] if s["strategy_name"] == "trend_following"]
        assert tf and tf[0]["closed"] >= 1
    finally:
        _cleanup()


def test_runner_runs_strategies_and_records(monkeypatch):
    _cleanup()
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw",
                        lambda tf, **k: {"direction": "buy", "adx_value": 30,
                                         "weekly_bias": "bullish", "rsi_value": 60, "score": 9})
    monkeypatch.setattr(indicators, "calc_all_indicators",
                        lambda d: {"valid": True, "ema": {"bias": "bullish", "valid": True},
                                   "atr": {"value": 2.0}, "current_price": 100.0, "support_resistance": {}})
    try:
        n = runner.run_and_record("2026-10-01", _TK, {"code": "9999", "name": "測試"},
                                  {"daily": {"closes": [100] * 10, "lows": [95, 96, 97, 96, 98]}}, {})
        assert n == 1
        from state_store import store
        with store._conn() as conn:
            names = [r[0] for r in conn.execute(
                "SELECT strategy_name FROM strategy_signals WHERE ticker=?", (_TK,)).fetchall()]
        assert "trend_following" in names
    finally:
        _cleanup()
