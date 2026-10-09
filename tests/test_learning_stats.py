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
