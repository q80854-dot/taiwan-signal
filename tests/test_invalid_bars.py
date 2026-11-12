"""無效（NaN／缺值）K 棒不得進入模擬（2026-11-12，由線上清洗日誌揭露）。

線上實例：3714.TW 2026-11-04 的假想單 mae_r=9e9、mfe_r=-9e9——資料源有整根為 NaN 的日 K，
NaN 與任何數比較皆為 False，出場條件永不成立，MFE/MAE 停在初始哨兵值而被寫進資料庫。
"""
import math

import execution as ex

NAN = float("nan")
_PNL = lambda e, x, d, s, days_held=0: (x - e) * s if d == "buy" else (e - x) * s


def _b(day, o, h, l, c):
    return {"bar_date": day, "open": o, "high": h, "low": l, "close": c}


def test_bar_ok():
    assert ex.bar_ok(_b("d", 10, 11, 9, 10))
    assert not ex.bar_ok(_b("d", NAN, NAN, NAN, NAN))
    assert not ex.bar_ok(_b("d", 10, None, 9, 10))
    assert not ex.bar_ok(_b("d", 10, 11, 9, 0))
    assert not ex.bar_ok({})


def test_single_exit_never_leaks_sentinel_when_all_bars_invalid():
    bars = [_b("2026-11-05", NAN, NAN, NAN, NAN), _b("2026-11-06", NAN, NAN, NAN, NAN)]
    out = ex.simulate_exit(planned_entry=56.7, entry_fill=56.7, stop=61.24, direction="sell", bars_after=bars,
                           bar_date="2026-11-04", expire_days=15, risk=4.54, tp1=50, tp2=47, tp3=43)
    assert out["mfe"] is None and out["mae"] is None and out["result"] is None      # 沒有有效資料：不給數字
    assert out["bars_held"] == 0


def test_single_exit_skips_invalid_bars_and_uses_valid_ones():
    bars = [_b("2026-10-02", NAN, NAN, NAN, NAN), _b("2026-10-05", 100, 106, 99.5, 105)]
    out = ex.simulate_exit(planned_entry=100, entry_fill=100, stop=95, direction="buy", bars_after=bars,
                           bar_date="2026-10-01", expire_days=15, risk=5, tp1=105, tp2=110, tp3=115)
    assert out["result"] == "tp1" and out["exit_date"] == "2026-10-05"
    assert out["mfe"] == 1.2 and out["mae"] == -0.1 and not math.isnan(out["mfe"])


def test_staged_exit_ignores_invalid_bars_and_never_returns_nan():
    bars = [_b("2026-10-02", 100, 106, 99.5, 104),            # TP1
            _b("2026-10-05", NAN, NAN, NAN, NAN),             # 無效：不成交、不結算
            _b("2026-10-06", 104, 104.5, 103, 104)]
    sim = ex.simulate_staged_exit(bars, entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
                                  shares=300, gen_date="2026-10-01", pnl_fn=_PNL, expire_days=15)
    assert [l["stage"] for l in sim["legs"]] == ["tp1", "expired"]
    assert sim["exit_date"] == "2026-10-06" and not math.isnan(sim["pnl"])
    tail_nan = ex.simulate_staged_exit(bars[:2], entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
                                       shares=300, gen_date="2026-10-01", pnl_fn=_PNL, expire_days=15)
    assert tail_nan["exit_date"] == "2026-10-02" and not math.isnan(tail_nan["pnl"])   # 以最後一根有效棒暫估
    none = ex.simulate_staged_exit([bars[1]], entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
                                   shares=300, gen_date="2026-10-01", pnl_fn=_PNL, expire_days=15)
    assert none is None                                        # 全是無效棒：沒有結果，不猜


def test_shadow_load_bars_drops_invalid_rows(monkeypatch):
    import shadow
    from state_store import store

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=()): return self
        def fetchall(self):
            return [{"ticker": "X.TW", "bar_date": "2026-10-01", "open": 10, "high": 11, "low": 9, "close": 10},
                    {"ticker": "X.TW", "bar_date": "2026-10-02", "open": NAN, "high": NAN, "low": NAN, "close": NAN},
                    {"ticker": "X.TW", "bar_date": "2026-10-03", "open": 10, "high": None, "low": 9, "close": 10}]
    monkeypatch.setattr(store, "_conn", lambda: _C())
    bars = shadow._load_bars(["X.TW"], "2026-09-01")
    assert [b["bar_date"] for b in bars["X.TW"]] == ["2026-10-01"]
