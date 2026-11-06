"""Phase 1 單一出場模擬器 execution.simulate_exit 行為測試（2026-11-06）。
鎖定保守規則：同K先停損、跳空穿越以開盤成交、TP3>TP2>TP1、逾期結算、R 以規劃風險為分母。"""
from datetime import datetime
import execution as ex


def _bar(d, o, h, l, c):
    return {"bar_date": d, "open": o, "high": h, "low": l, "close": c}


# 進場 100、停損 95（risk=5），tp1=105 tp2=110 tp3=120
BASE = dict(planned_entry=100.0, entry_fill=100.0, stop=95.0, direction="buy",
            bar_date="2026-10-01", expire_days=15, risk=5.0, tp1=105.0, tp2=110.0, tp3=120.0,
            now=datetime(2026, 10, 10))


def test_hits_tp1():
    bars = [_bar("2026-10-02", 100, 106, 99, 105)]
    o = ex.simulate_exit(bars_after=bars, **BASE)
    assert o["result"] == "tp1" and o["exit_price"] == 105.0 and o["bars_held"] == 1


def test_same_bar_sl_and_tp_is_conservative_sl_first():
    # 同一根同時觸及停損(95)與停利(105) → 保守取停損
    bars = [_bar("2026-10-02", 100, 106, 94, 100)]
    o = ex.simulate_exit(bars_after=bars, **BASE)
    assert o["result"] == "sl" and o["exit_price"] == 95.0


def test_gap_through_stop_fills_at_open():
    # 開盤就跳空跌破停損(開 90 < 停損 95) → 以開盤 90 成交，不是停損價 95
    bars = [_bar("2026-10-02", 90, 92, 88, 90)]
    o = ex.simulate_exit(bars_after=bars, **BASE)
    assert o["result"] == "sl" and o["exit_price"] == 90.0


def test_tp_priority_tp3_over_tp2_tp1():
    bars = [_bar("2026-10-02", 100, 121, 99, 120)]
    o = ex.simulate_exit(bars_after=bars, **BASE)
    assert o["result"] == "tp3" and o["exit_price"] == 120.0


def test_expiry_marks_expired_at_last_close():
    # 從不觸及停損停利，且已逾期 → expired、以最新收盤結算
    bars = [_bar(f"2026-10-{d:02d}", 100, 104, 96, 101) for d in range(2, 5)]
    o = ex.simulate_exit(bars_after=bars, **dict(BASE, now=datetime(2026, 10, 20)))  # 距訊號日 19 天 ≥ 15
    assert o["result"] == "expired" and o["exit_price"] == 101
    assert o["exit_date"] == "2026-10-04"


def test_not_yet_expired_stays_open():
    bars = [_bar("2026-10-02", 100, 104, 96, 101)]
    o = ex.simulate_exit(bars_after=bars, **dict(BASE, now=datetime(2026, 10, 3)))
    assert o["result"] is None            # 才隔 2 天 < expire_days，維持未出場


def test_r_uses_planned_risk_and_entry_fill():
    # entry_fill=100, risk=5；r1 = (close-entry)/risk
    bars = [_bar("2026-10-02", 100, 104, 99, 103)]
    o = ex.simulate_exit(bars_after=bars, **BASE)
    assert abs(o["r1"] - (103 - 100) / 5) < 1e-9


def test_short_direction_mirror():
    # 做空：進場100 停損105(risk=5) tp1=95；跌到95 觸及停利
    bars = [_bar("2026-10-02", 100, 101, 94, 95)]
    o = ex.simulate_exit(planned_entry=100.0, entry_fill=100.0, stop=105.0, direction="sell",
                         bar_date="2026-10-01", expire_days=15, risk=5.0, tp1=95.0,
                         bars_after=bars, now=datetime(2026, 10, 10))
    assert o["result"] == "tp1" and o["exit_price"] == 95.0


def test_mfe_mae_bounded_and_signed():
    bars = [_bar("2026-10-02", 100, 108, 97, 102)]
    o = ex.simulate_exit(bars_after=bars, **BASE)
    # mfe = (108-100)/5 = 1.6；mae = (97-100)/5 = -0.6
    assert abs(o["mfe"] - 1.6) < 1e-9 and abs(o["mae"] + 0.6) < 1e-9


def test_empty_bars_returns_none_result():
    o = ex.simulate_exit(bars_after=[], **BASE)
    assert o["result"] is None and o["bars_held"] == 0


def test_sim_version_tagged():
    o = ex.simulate_exit(bars_after=[_bar("2026-10-02", 100, 101, 99, 100)], **BASE)
    assert o["sim_version"] == ex.SIM_VERSION
