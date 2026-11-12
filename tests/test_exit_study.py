"""出場方式研究工具（research/exit_study.py）：方案事前寫死、基準與正式狀態機一致、移動停損無未來函數。"""
import execution
import shadow
from research import exit_study as es


def _bars(seq, start_day=2):
    """seq: [(open, high, low, close)]；日期自 2026-03-02 起逐日（不跳假日，測試只看日曆天差）。"""
    return [{"bar_date": f"2026-03-{start_day + i:02d}", "open": o, "high": h, "low": l, "close": c}
            for i, (o, h, l, c) in enumerate(seq)]


def _sig(**k):
    s = {"ticker": "9999.TW", "signal_date": "2026-03-01", "direction": "buy", "fill_date": "2026-03-02",
         "fill": 100.0, "entry_plan": 100.0, "stop": 95.0, "tp1": 107.5, "tp2": 112.5, "tp3": 120.0,
         "sl_atr": 2.0, "prev_close": 100.0}
    s.update(k)
    return s


def _v(vid):
    return next(v for v in es.VARIANTS if v["id"] == vid)


def test_variants_are_predeclared_and_baseline_is_current_exit():
    assert [v["id"] for v in es.VARIANTS] == [f"V{i}" for i in range(8)]
    assert _v("V0") == {"id": "V0", "name": _v("V0")["name"], "expire": 15, "ladder": True}
    import config
    assert _v("V0")["expire"] == config.CIRCUIT_BREAKER["signal_expire_days"]


def test_baseline_matches_the_production_staged_exit():
    seq = [(100, 103, 99, 102), (102, 108, 101, 107), (107, 113, 106, 112), (112, 114, 99, 100), (100, 101, 98, 99)]
    bars, pnl = _bars(seq), shadow._shadow_staged_pnl_factory()
    ref = execution.simulate_staged_exit(bars, 100.0, 95.0, 107.5, 112.5, 120.0, "buy", es.SHARES, "2026-03-01",
                                         pnl, expire_days=15, prev_close=100.0)
    out = es.simulate(_v("V0"), _sig(), bars, pnl, es._position_cls())
    assert abs(out["r"] - ref["pnl"] / (5.0 * es.SHARES)) < 1e-4 and out["exit_date"] == ref["exit_date"]   # 正式版把損益四捨五入到元


def test_trailing_stop_uses_only_past_bars_and_takes_effect_next_bar():
    # ATR = 5/2 = 2.5；3 ATR = 7.5。第 1 根高 110 → 停損升到 102.5，但「同一根」低 101 不得觸發
    seq = [(100, 110, 101, 109), (109, 109.5, 103, 104), (104, 105, 102, 102.2)]
    out = es.simulate(_v("V5"), _sig(), _bars(seq), shadow._shadow_staged_pnl_factory(), es._position_cls())
    assert out["exit_date"] == "2026-03-04" and out["last"] == "stop"       # 第 3 根才跌破 102.5
    assert 0.3 < out["r"] < 0.5                                             # 約 (102.5−100)/5 再扣成本


def test_trailing_never_loosens_the_initial_stop():
    seq = [(100, 100.5, 96, 96.5), (96.5, 97, 94, 94.5)]                    # 100.5−7.5=93 < 95：停損維持 95
    out = es.simulate(_v("V5"), _sig(), _bars(seq), shadow._shadow_staged_pnl_factory(), es._position_cls())
    assert out["exit_date"] == "2026-03-03" and -1.2 < out["r"] < -1.0


def test_time_stop_exits_dead_trades_but_not_ones_that_reached_tp1():
    flat = [(100, 101, 99, 100)] * 8
    out = es.simulate(_v("V7"), _sig(), _bars(flat), shadow._shadow_staged_pnl_factory(), es._position_cls())
    assert out["last"] == "time_stop" and out["hold_days"] == 7 and -0.2 < out["r"] < 0
    moved = [(100, 108, 99.5, 107)] + [(107, 108, 106, 107)] * 13 + [(107, 108, 106, 107)]
    out = es.simulate(_v("V7"), _sig(), _bars(moved), shadow._shadow_staged_pnl_factory(), es._position_cls())
    assert out["last"] == "expired" and out["hold_days"] >= 15              # 已到 TP1：照原本 15 天到期


def test_position_still_open_at_data_end_is_excluded_not_estimated():
    out = es.simulate(_v("V3"), _sig(), _bars([(100, 101, 99, 100)] * 5), shadow._shadow_staged_pnl_factory(),
                      es._position_cls())
    assert out == {"open_end": True}                                        # 60 天方案只有 5 根：不以暫估值當結果


def test_summary_selection_rule_requires_train_then_test():
    import random
    rng = random.Random(1)
    days = [f"20{y}-{m:02d}-{d:02d}" for y in (22, 23, 24, 25) for m in range(1, 13) for d in (5, 15, 25)]
    res = {v["id"]: [] for v in es.VARIANTS}
    for d in days:
        for k in range(3):
            base = rng.gauss(-0.1, 1.0)
            for v in es.VARIANTS:
                bump = 0.0
                if v["id"] == "V2":
                    bump = 0.3                                  # 訓練、驗證期都穩定較好
                elif v["id"] == "V5":
                    bump = 0.5 if d < es.SPLIT else -0.5        # 只在訓練期好：樣本外反轉
                res[v["id"]].append((d, f"T{k}", base + bump + rng.gauss(0, 0.05), 10, d, "stop"))
    rep = es.summarize(res)
    assert rep["verdict"]["selected"] == "V5" and rep["verdict"]["result"] == "not_confirmed"
    res["V5"] = [(d, t, r - (0.5 if d < es.SPLIT else -0.5), h, e, l) for d, t, r, h, e, l in res["V5"]]
    rep = es.summarize(res)
    assert rep["verdict"]["selected"] == "V2" and rep["verdict"]["result"] == "confirmed"
    res["V2"] = [(d, t, r - 0.3, h, e, l) for d, t, r, h, e, l in res["V2"]]
    assert es.summarize(res)["verdict"]["result"] == "none_selected"


def test_no_overlap_total_skips_signals_while_ticker_is_held():
    rows = [("2026-01-01", "A", 1.0, 30, "2026-01-31", "stop"), ("2026-01-10", "A", 5.0, 5, "2026-01-15", "stop"),
            ("2026-02-01", "A", 2.0, 5, "2026-02-06", "stop"), ("2026-01-10", "B", 0.5, 5, "2026-01-15", "stop")]
    assert es._no_overlap_total(rows) == (3, 3.5)
