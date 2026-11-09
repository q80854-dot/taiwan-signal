"""每日橫斷面 IC → ICIR（§三 3.3 第一刀，2026-11-09）。

驗證：穩定的日度橫斷面預測因子→高 mean_ic、ICIR 有限且大、正 IC 天數比高；純雜訊→ICIR 近 0；
天數不足不給（None）；champion_r 有值才出 champion 區塊；多空分開。
"""
import random
import learning as L


def _day_rows(day, n, factor_fn, rng, direction="buy", champion=False):
    rows = []
    for _ in range(n):
        rm = rng.uniform(-2, 2)
        f = {"good": factor_fn(rm, rng), "noise": rng.uniform(-1, 1)}
        row = {"kind": "candidate", "status": "closed", "direction": direction,
               "bar_date": day, "result": "tp1", "r_multiple": rm, "f": f}
        if champion:
            row["champion_r"] = rm * 0.8          # 分批淨 R 與單次同向、略小
        rows.append(row)
    return rows


def test_daily_icir_detects_stable_factor(monkeypatch):
    rng = random.Random(0)
    rows = []
    for d in range(1, 9):                           # 8 個交易日、各 6 檔
        # good ≈ rm（日內高排序相關）但帶日度小擾動 → 每日 IC 高且有變異（std>0、ICIR 有限）
        rows += _day_rows(f"2026-10-{d:02d}", 6,
                          lambda rm, r: rm + r.uniform(-0.3, 0.3), rng)
    monkeypatch.setattr(L, "_rows", lambda: rows)
    rep = L.factor_ic_report()
    assert rep["ok"] is True
    daily = rep["daily"]["research"]["buy"]
    facs = {d["factor"]: d for d in daily["factors"]}
    assert "good" in facs
    g = facs["good"]
    assert g["n_days"] >= 5
    assert g["mean_ic"] > 0.7                       # 日內強預測
    assert g["icir"] is not None and g["icir"] > 1  # 穩定（mean 遠大於 std）
    assert g["pct_pos_days"] == 100                 # 每日 IC 皆為正
    if "noise" in facs and facs["noise"]["icir"] is not None:
        assert abs(g["icir"]) > abs(facs["noise"]["icir"])


def test_daily_icir_insufficient_days(monkeypatch):
    rng = random.Random(1)
    rows = []
    for d in range(1, 4):                            # 只有 3 天 < _ICIR_MIN_DAYS(5)
        rows += _day_rows(f"2026-10-{d:02d}", 6, lambda rm, r: rm, rng)
    monkeypatch.setattr(L, "_rows", lambda: rows)
    daily = L.factor_ic_report()["daily"]["research"]["buy"]
    assert daily["factors"] == [] and daily["n_days_max"] == 0


def test_daily_icir_breadth_filter(monkeypatch):
    # 每天只有 2 檔（< _IC_DAY_MIN_BREADTH=4）→ 無任一日 IC → 因子不出現
    rng = random.Random(2)
    rows = []
    for d in range(1, 10):
        rows += _day_rows(f"2026-10-{d:02d}", 2, lambda rm, r: rm, rng)
    monkeypatch.setattr(L, "_rows", lambda: rows)
    daily = L.factor_ic_report()["daily"]["research"]["buy"]
    assert daily["factors"] == []


def test_daily_icir_champion_block_present(monkeypatch):
    rng = random.Random(3)
    rows = []
    for d in range(1, 9):
        rows += _day_rows(f"2026-10-{d:02d}", 6,
                          lambda rm, r: rm + r.uniform(-0.3, 0.3), rng, champion=True)
    monkeypatch.setattr(L, "_rows", lambda: rows)
    rep = L.factor_ic_report()
    assert "champion" in rep["daily"]
    cfacs = {d["factor"]: d for d in rep["daily"]["champion"]["buy"]["factors"]}
    assert "good" in cfacs and cfacs["good"]["n_days"] >= 5


def test_daily_icir_no_champion_block_when_absent(monkeypatch):
    rng = random.Random(4)
    rows = []
    for d in range(1, 9):
        rows += _day_rows(f"2026-10-{d:02d}", 6, lambda rm, r: rm + r.uniform(-0.3, 0.3), rng)
    monkeypatch.setattr(L, "_rows", lambda: rows)
    assert "champion" not in L.factor_ic_report()["daily"]
