"""Phase 1：分批出場模擬器 execution.simulate_staged_exit 特徵化測試（2026-11-08）。
鎖定與原 backtester.simulate_staged_exit 完全一致的保守行為：TP1/TP2/TP3 各出 1/3、
TP1→保本、TP2→移至 TP1、同 K 先停損、跳空以開盤成交、末段逾期以收盤結算。"""
import execution as ex
import backtester as bt


def _bar(d, o, h, l, c):
    return {"bar_date": d, "open": o, "high": h, "low": l, "close": c}


def _pnl(entry, px, direction, shares, days_held=0):
    sgn = 1 if direction == "buy" else -1
    return round(sgn * (px - entry) * shares, 2)


BASE = dict(entry=100.0, sl=95.0, tp1=105.0, tp2=110.0, tp3=120.0,
            direction="buy", shares=30, gen_date="2026-10-01")


def _run(bars):
    return ex.simulate_staged_exit(bars=bars, pnl_fn=_pnl, **BASE)


def test_all_three_tps():
    bars = [_bar("2026-10-02", 100, 106, 99, 105),   # tp1
            _bar("2026-10-03", 105, 111, 104, 110),  # tp2
            _bar("2026-10-04", 110, 121, 109, 120)]  # tp3
    o = _run(bars)
    assert o["result"] == "tp3"
    stages = [l["stage"] for l in o["legs"]]
    assert stages == ["tp1", "tp2", "tp3"]
    assert all(l["shares"] == 10 for l in o["legs"])


def test_tp1_then_breakeven_stop():
    # TP1 觸及後停損移到成本(100)；次日跌破 100 → 其餘 2/3 以 100 停損出場
    bars = [_bar("2026-10-02", 100, 106, 99, 105),   # tp1 → SL 移至 100
            _bar("2026-10-03", 100, 101, 98, 99)]    # lo98 < 100 → 停損
    o = _run(bars)
    assert o["result"] == "tp1"                       # 最高觸及停利為 tp1
    stages = [(l["stage"], l["shares"], l["price"]) for l in o["legs"]]
    assert stages == [("tp1", 10, 105.0), ("stop", 20, 100.0)]


def test_same_bar_stop_first():
    # 同一根同時觸及 tp1(106) 與停損(94<95) → 先判停損、全出，無 tp leg
    bars = [_bar("2026-10-02", 100, 106, 94, 100)]
    o = _run(bars)
    assert o["result"] == "sl"
    assert [l["stage"] for l in o["legs"]] == ["stop"]
    assert o["legs"][0]["shares"] == 30 and o["legs"][0]["price"] == 95.0


def test_gap_through_stop_fills_at_open():
    bars = [_bar("2026-10-02", 90, 92, 88, 90)]       # 開盤 90 < 停損 95
    o = _run(bars)
    assert o["result"] == "sl" and o["legs"][0]["price"] == 90.0


def test_expired_remainder_at_last_close():
    bars = [_bar("2026-10-02", 100, 104, 96, 101),
            _bar("2026-10-03", 101, 104, 97, 102)]    # 從不觸及 tp/sl
    o = _run(bars)
    assert o["result"] == "expired"
    assert o["legs"][-1]["stage"] == "expired" and o["legs"][-1]["shares"] == 30
    assert o["legs"][-1]["price"] == 102


def test_backtester_delegates_identically(monkeypatch):
    """backtester.simulate_staged_exit 應委派 execution 且結果一致（成本走 calc_tw_pnl）。"""
    bars = [_bar("2026-10-02", 100, 106, 99, 105), _bar("2026-10-03", 105, 121, 104, 120)]
    a = bt.simulate_staged_exit(bars, 100.0, 95.0, 105.0, 110.0, 120.0, "buy", 30, "2026-10-01")
    b = ex.simulate_staged_exit(bars=bars, entry=100.0, sl=95.0, tp1=105.0, tp2=110.0, tp3=120.0,
                                direction="buy", shares=30, gen_date="2026-10-01", pnl_fn=bt.calc_tw_pnl)
    assert a["result"] == b["result"] and a["pnl"] == b["pnl"] and a["legs"] == b["legs"]
    assert a["sim_version"] == ex.STAGED_SIM_VERSION


def test_empty_bars_returns_none():
    assert _run([]) is None
