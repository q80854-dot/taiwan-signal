"""策略學習統計升級（2026-10-09）：交易日叢集拔靴信賴區間、兩樣本拔靴差異、Benjamini-Hochberg FDR。"""
import random
import learning as L


def _rows(days, per_day, value_fn):
    return [{"bar_date": f"2026-10-{d:02d}", "r_multiple": value_fn(d, i)}
            for d in range(1, days + 1) for i in range(per_day)]


def test_boot_ci_brackets_true_mean():
    rows = _rows(12, 5, lambda d, i: 0.5)
    m, lo, hi = L._boot_ci(rows)
    assert m == 0.5 and lo is not None and lo <= 0.5 <= hi


def test_boot_diff_detects_clear_separation():
    a = _rows(12, 4, lambda d, i: 1.0)
    b = _rows(12, 4, lambda d, i: -1.0)
    diff, p, z = L._boot_diff(a, b)
    assert diff > 1.5 and p < 0.05 and z > 0 and abs(z) <= 99


def test_boot_diff_insufficient_sample_is_not_significant():
    a = _rows(1, 1, lambda d, i: 1.0)
    b = _rows(1, 1, lambda d, i: -1.0)
    diff, p, z = L._boot_diff(a, b)
    assert p == 1.0


def test_bh_controls_family():
    # 一個強訊號 + 一堆雜訊 p 值：BH 應只選出強的，不會把 0.04 這種邊緣值全放行
    ps = [0.0001] + [0.04, 0.2, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99]
    flags = L._bh_reject(ps, 0.10)
    assert flags[0] is True and sum(flags) <= 2
    # 全部不顯著 → 全部 False
    assert L._bh_reject([0.3, 0.4, 0.8], 0.10) == [False, False, False]


def test_bh_all_significant():
    assert L._bh_reject([0.001, 0.002, 0.003], 0.10) == [True, True, True]


# ── 2026-10-15 分析能力提升：出場原因分布、MAE、平均持有天數 ──
def test_stats_exits_mae_and_hold():
    rows = [
        {"result": "tp1", "r_multiple": 1.0, "mae_r": -0.3, "bars_held": 4},
        {"result": "tp2", "r_multiple": 2.0, "mae_r": -0.2, "bars_held": 6},
        {"result": "sl",  "r_multiple": -1.0, "mae_r": -1.0, "bars_held": 2},
        {"result": "expired", "r_multiple": 0.2, "mae_r": -0.5, "bars_held": 15},
    ]
    st = L._stats(rows)
    assert st["n"] == 4 and st["win"] == 2 and st["loss"] == 1 and st["expired"] == 1
    assert st["exits"] == {"tp1": 1, "tp2": 1, "sl": 1, "expired": 1}
    assert st["mae"] == round((-0.3 - 0.2 - 1.0 - 0.5) / 4, 2)
    assert st["avg_bars_held"] == round((4 + 6 + 2 + 15) / 4, 1)


def test_stats_empty_safe():
    st = L._stats([])
    assert st == {"n": 0}


def test_new_dims_present():
    names = {name for name, _ in L._dims()}
    for expect in ("相對大盤強弱(20日)", "連漲天數", "收盤區間位置", "量能趨勢(5日)", "大盤型態", "月份"):
        assert expect in names


# ── 2026-10-16 更詳細的統計 + 大數據蒙地卡羅 ──
def _closed(rs):
    return [{"bar_date": f"2026-10-{1 + i // 5:02d}", "status": "closed", "id": i,
             "result": ("tp1" if r > 0 else "sl"), "r_multiple": r} for i, r in enumerate(rs)]


def test_stats_extra_metrics():
    rows = _closed([2.0, 2.0, -1.0, -1.0, 2.0, -1.0])     # 3 勝(各+2R)、3 敗(各-1R)
    st = L._stats(rows)
    assert st["profit_factor"] == 2.0                      # 6 / 3
    assert st["payoff"] == 2.0                             # 2 / 1
    assert st["avg_win_r"] == 2.0 and st["avg_loss_r"] == -1.0
    assert st["best_r"] == 2.0 and st["worst_r"] == -1.0
    assert st["max_consec_loss"] == 2


def test_montecarlo_positive_edge_mostly_profits():
    rows = _closed([1.0] * 40 + [-0.5] * 20)              # 明顯正期望值
    mc = L._mc_equity(rows, n_sims=500)
    assert mc["ready"] is True and mc["n"] == 60
    assert mc["prob_profit"] > 95 and mc["total_r_med"] > 0
    assert mc["maxdd_med"] <= 0


def test_montecarlo_needs_min_sample():
    mc = L._mc_equity(_closed([1.0] * 5), n_sims=100)
    assert mc["ready"] is False


def test_equity_curve_and_monthly():
    rows = _closed([1.0, -0.5, 1.0, 1.0])
    eq = L._equity_curve(rows)
    assert eq[-1]["cum_r"] == 2.5                          # 1 -0.5 +1 +1
    mo = L._monthly(rows)
    assert mo and mo[0]["month"] == "2026-10" and mo[0]["n"] == 4
