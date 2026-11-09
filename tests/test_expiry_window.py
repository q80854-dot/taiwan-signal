"""Phase B 交易模擬一致性（2026-11-09，依第二階段審查 §四「到期規則」「同根 K 標記」）。

鎖定查證屬實的三項 look-ahead / 標記問題的修正：
1) simulate_exit / simulate_staged_exit：到期日（日曆天 >= expire_days）之後才觸及的停利/停損
   不再計入出場——否則模擬會把到期後的 TP 誤算成勝利（高估勝率/報酬）。
2) tp1_before_sl：同一根 K 棒同時觸及 TP1 與 SL 時，依「同根先判停損」保守慣例視為 SL 先（=0）。
"""
from datetime import datetime, timedelta
import execution


def _day(n):
    return (datetime(2026, 1, 1) + timedelta(days=n)).strftime("%Y-%m-%d")


def _bar(n, o, h, l, c):
    return {"bar_date": _day(n), "open": o, "high": h, "low": l, "close": c}


def test_simulate_exit_expiry_excludes_late_tp():
    # 訊號日 2026-01-01，expire_days=15。前 15 日皆未觸及 TP/SL，第 20 日才噴到 TP1。
    bars = [_bar(n, 98, 99, 97, 98) for n in range(2, 20)]
    bars.append(_bar(20, 98, 106, 97, 105))      # age=19：若無到期窗會被誤計 tp1
    out = execution.simulate_exit(
        planned_entry=100, entry_fill=100, stop=95, direction="buy",
        bars_after=bars, bar_date="2026-01-01", expire_days=15, risk=5,
        tp1=105, tp2=108, tp3=112)
    assert out["result"] == "expired"            # 到期結算，不是 tp1
    assert out["exit_date"] == _day(15)          # 首根 age>=15（2026-01-16）以收盤結算
    assert out["exit_price"] == 98


def test_simulate_exit_long_window_counts_tp():
    # 同樣的 K 棒，但到期窗拉長到 40 天 → 第 20 日的 TP1 應被計入（對照組）。
    bars = [_bar(n, 98, 99, 97, 98) for n in range(2, 20)]
    bars.append(_bar(20, 98, 106, 97, 105))
    out = execution.simulate_exit(
        planned_entry=100, entry_fill=100, stop=95, direction="buy",
        bars_after=bars, bar_date="2026-01-01", expire_days=40, risk=5,
        tp1=105, tp2=108, tp3=112)
    assert out["result"] == "tp1"


def test_tp1_before_sl_same_bar_is_conservative():
    # 單根 K 同時觸及 TP1(高>=105) 與 SL(低<=95) → 視為 SL 先（tp1_before_sl=0），結果為 sl。
    bars = [_bar(2, 100, 106, 94, 96)]
    out = execution.simulate_exit(
        planned_entry=100, entry_fill=100, stop=95, direction="buy",
        bars_after=bars, bar_date="2026-01-01", expire_days=15, risk=5,
        tp1=105, tp2=108, tp3=112)
    assert out["result"] == "sl"
    assert out["tp1_before_sl"] == 0


def _pnl(entry, exit_price, direction, shares, days_held=0):
    sgn = 1 if direction == "buy" else -1
    return sgn * (exit_price - entry) * shares


def test_staged_expiry_settles_remaining_not_late_tp():
    # TP1 於第 2 日成交 1/3；其後盤整；第 20 日才噴到 TP2。到期窗=15 → 剩餘 2/3 應於到期日以收盤
    # 結算 expired，result 停在 tp1，不得出現 tp2 leg。
    bars = [_bar(2, 100, 106, 99, 104)]          # 觸及 tp1=105
    bars += [_bar(n, 103, 104, 102, 103) for n in range(3, 20)]
    bars.append(_bar(20, 103, 112, 102, 111))    # 若無到期窗會誤計 tp2=110
    sim = execution.simulate_staged_exit(
        bars, entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
        shares=300, gen_date="2026-01-01", pnl_fn=_pnl, expire_days=15)
    stages = [l["stage"] for l in sim["legs"]]
    assert "tp2" not in stages
    assert "expired" in stages
    assert sim["result"] == "tp1"


def test_staged_no_expire_window_counts_late_tp():
    # 對照組：expire_days=None（不設窗）→ 第 20 日 TP2 應被計入。
    bars = [_bar(2, 100, 106, 99, 104)]
    bars += [_bar(n, 103, 104, 102, 103) for n in range(3, 20)]
    bars.append(_bar(20, 103, 112, 102, 111))
    sim = execution.simulate_staged_exit(
        bars, entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy",
        shares=300, gen_date="2026-01-01", pnl_fn=_pnl, expire_days=None)
    stages = [l["stage"] for l in sim["legs"]]
    assert "tp2" in stages
