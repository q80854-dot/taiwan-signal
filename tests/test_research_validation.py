"""研究驗證工具（research/validation.py）：DSR 與 PBO。用已知答案的合成資料驗證方向正確。"""
import random

from research import validation as rv


def _noise(n, seed, mu=0.0):
    r = random.Random(seed)
    return [r.gauss(mu, 1.0) for _ in range(n)]


def test_expected_max_sharpe_grows_with_number_of_trials():
    a, b, c = (rv.expected_max_sharpe(n, 0.01) for n in (2, 20, 200))
    assert 0 < a < b < c
    assert rv.expected_max_sharpe(1, 0.01) == 0.0 and rv.expected_max_sharpe(50, 0.0) == 0.0


def test_dsr_is_high_for_a_real_edge_and_drops_when_many_trials_were_run():
    xs = _noise(1500, 1, mu=0.15)
    few = rv.deflated_sharpe(xs, n_trials=2, sr_variance=0.001)
    many = rv.deflated_sharpe(xs, n_trials=2000, sr_variance=0.01)
    assert few["dsr"] > 0.99 and many["dsr"] < few["dsr"]
    flat = rv.deflated_sharpe(_noise(1500, 2), n_trials=20, sr_variance=0.001)
    assert flat["dsr"] < 0.6                                  # 沒有真本事：分不出來
    assert rv.deflated_sharpe([0.1, 0.2], 5, 0.01)["dsr"] is None


def test_pbo_is_about_half_for_pure_noise_and_low_when_one_strategy_is_truly_better():
    noise = {f"s{i}": _noise(1000, 10 + i) for i in range(8)}
    p = rv.pbo_cscv(noise)
    assert p["n_splits"] == 252 and 0.25 < p["pbo"] < 0.75   # 全是雜訊：挑最好的沒有意義
    real = dict(noise, good=_noise(1000, 99, mu=0.25))
    q = rv.pbo_cscv(real)
    assert q["pbo"] < 0.1 and max(q["best_is_counts"], key=q["best_is_counts"].get) == "good"
    assert rv.pbo_cscv({"a": [1.0] * 5, "b": [2.0] * 5})["pbo"] is None      # 資料太短不算


def test_daily_series_averages_same_day_trades():
    days, xs = rv.daily_series([("2026-01-02", 1.0), ("2026-01-01", 3.0), ("2026-01-02", -1.0)])
    assert days == ["2026-01-01", "2026-01-02"] and xs == [3.0, 0.0]


def test_reality_check_separates_a_real_improvement_from_the_best_of_many_noise_variants():
    bench = _noise(1200, 500)
    noise = {f"v{i}": [b + e for b, e in zip(bench, _noise(1200, 600 + i))] for i in range(8)}
    r = rv.reality_check(bench, noise, n_boot=500)
    assert r["p_value"] > 0.05                               # 8 個純雜訊方案裡最好的：不算真的比較好
    real = dict(noise, good=[b + 0.2 + 0.3 * e for b, e in zip(bench, _noise(1200, 777))])
    q = rv.reality_check(bench, real, n_boot=500)
    assert q["best"] == "good" and q["p_value"] < 0.01
    assert rv.reality_check([0.0] * 5, {"a": [1.0] * 5})["p_value"] is None
