"""忠實 1/3 分批出場模擬（2026-10-09）：TP1/TP2/TP3 各出 1/3，TP1 後停損移至成本、TP2 後移至 TP1，
同根 K 棒停損優先於停利。"""
from backtester import simulate_staged_exit

GEN = "2026-10-08"


def _bar(d, o, h, l, c):
    return {"bar_date": d, "open": o, "high": h, "low": l, "close": c}


def test_all_three_targets_hit_is_full_win():
    # 進場 100、停損 95、TP 110/120/130；分三天分別摸到 110/120/130
    bars = [_bar("2026-10-09", 101, 111, 100, 110),
            _bar("2026-10-12", 112, 121, 111, 120),
            _bar("2026-10-13", 122, 131, 121, 130)]
    sim = simulate_staged_exit(bars, 100, 95, 110, 120, 130, "buy", 300, GEN)
    assert sim["result"] == "tp3"
    assert [l["stage"] for l in sim["legs"]] == ["tp1", "tp2", "tp3"]
    assert sim["pnl"] > 0


def test_tp1_then_breakeven_stop_on_remaining():
    # 第一天摸到 TP1（出 1/3，停損移到成本 100）；第二天跌破 100 → 其餘 2/3 以成本價出場
    bars = [_bar("2026-10-09", 101, 111, 100, 109),
            _bar("2026-10-12", 100, 101, 96, 98)]
    sim = simulate_staged_exit(bars, 100, 95, 110, 120, 130, "buy", 300, GEN)
    stages = [l["stage"] for l in sim["legs"]]
    assert stages[0] == "tp1" and "stop" in stages
    # 停損腳成交價為成本價 100（非原始停損 95）：移動停損生效
    stop_leg = next(l for l in sim["legs"] if l["stage"] == "stop")
    assert abs(stop_leg["price"] - 100) < 1e-6
    assert sim["result"] == "tp1"   # 觸及的最高停利


def test_stop_before_any_tp_is_full_loss():
    bars = [_bar("2026-10-09", 99, 99, 94, 95)]   # 開盤就跌、摜破停損 95
    sim = simulate_staged_exit(bars, 100, 95, 110, 120, 130, "buy", 300, GEN)
    assert sim["result"] == "sl"
    assert all(l["stage"] == "stop" for l in sim["legs"])
    assert sim["pnl"] < 0


def test_gap_through_stop_fills_at_open():
    bars = [_bar("2026-10-09", 90, 92, 88, 89)]   # 跳空開 90，遠低於停損 95
    sim = simulate_staged_exit(bars, 100, 95, 110, 120, 130, "buy", 300, GEN)
    stop_leg = sim["legs"][0]
    assert stop_leg["stage"] == "stop" and abs(stop_leg["price"] - 90) < 1e-6   # 以開盤價 90 成交，不是 95


def test_same_bar_stop_takes_priority_over_tp():
    # 同一天同時觸及停損 95 與 TP1 110 → 保守判停損
    bars = [_bar("2026-10-09", 100, 111, 94, 100)]
    sim = simulate_staged_exit(bars, 100, 95, 110, 120, 130, "buy", 300, GEN)
    assert sim["result"] == "sl"


def test_short_direction_targets_below_entry():
    # 放空：進場 100、停損 105、TP 90/80/70
    bars = [_bar("2026-10-09", 99, 100, 89, 90),
            _bar("2026-10-12", 88, 89, 79, 80),
            _bar("2026-10-13", 78, 79, 69, 70)]
    sim = simulate_staged_exit(bars, 100, 105, 90, 80, 70, "sell", 300, GEN)
    assert sim["result"] == "tp3" and sim["pnl"] > 0
