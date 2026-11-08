"""P1-3 因子 Rank IC 測試（2026-11-08）：量化因子對淨 R 的預測力。
確保強預測因子被抓到（高 |IC|、顯著）、純雜訊因子 IC 近 0、樣本不足誠實標示、排除 bad_risk。"""
import random
import learning as L


def _rows(specs):
    """specs: list of dict(f=..., r_multiple=..., direction=..., ...)，補齊必要欄位。"""
    out = []
    for i, s in enumerate(specs):
        base = {"kind": "candidate", "status": "closed", "direction": "buy",
                "bar_date": f"2026-10-{(i % 25) + 1:02d}", "result": "tp1"}
        base.update(s)
        out.append(base)
    return out


def test_norm_sf_basic():
    assert abs(L._norm_sf(0) - 0.5) < 1e-9
    assert L._norm_sf(3) < 0.01 and L._norm_sf(-3) > 0.99


def test_strong_factor_detected(monkeypatch):
    # good 因子與淨 R 完全正相關；noise 因子隨機 → good 的 |IC| 應遠大、且顯著
    rng = random.Random(0)
    specs = []
    for i in range(80):
        rm = rng.uniform(-2, 2)
        specs.append({"r_multiple": rm, "f": {"good": rm, "noise": rng.uniform(-1, 1)}})
    monkeypatch.setattr(L, "_rows", lambda: _rows(specs))
    rep = L.factor_ic_report()
    assert rep["ok"] is True
    facs = {d["factor"]: d for d in rep["buy"]["factors"]}
    assert "good" in facs and "noise" in facs
    assert abs(facs["good"]["ic"]) > 0.9 and facs["good"]["significant"] is True
    assert abs(facs["noise"]["ic"]) < abs(facs["good"]["ic"])
    # 排序：good 應排在 noise 前
    order = [d["factor"] for d in rep["buy"]["factors"]]
    assert order.index("good") < order.index("noise")


def test_negative_ic_sign(monkeypatch):
    rng = random.Random(1)
    specs = []
    for i in range(60):
        rm = rng.uniform(-2, 2)
        specs.append({"r_multiple": rm, "f": {"inv": -rm}})   # 完全負相關
    monkeypatch.setattr(L, "_rows", lambda: _rows(specs))
    rep = L.factor_ic_report()
    facs = {d["factor"]: d for d in rep["buy"]["factors"]}
    assert facs["inv"]["ic"] < -0.9


def test_insufficient_sample(monkeypatch):
    specs = [{"r_multiple": 1.0, "f": {"x": 1.0}} for _ in range(10)]
    monkeypatch.setattr(L, "_rows", lambda: _rows(specs))
    rep = L.factor_ic_report()
    assert rep["buy"]["insufficient"] is True and rep["buy"]["n"] == 10


def test_excludes_bad_risk_and_score(monkeypatch):
    rng = random.Random(2)
    specs = []
    for i in range(50):
        rm = rng.uniform(-2, 2)
        specs.append({"r_multiple": rm, "f": {"a": rm, "score": rm, "month": 11}})
    # 一筆 bad_risk 應被排除
    specs.append({"r_multiple": 9e9, "result": "bad_risk", "f": {"a": 9e9, "score": 9e9}})
    monkeypatch.setattr(L, "_rows", lambda: _rows(specs))
    rep = L.factor_ic_report()
    facs = {d["factor"] for d in rep["buy"]["factors"]}
    assert "a" in facs
    assert "score" not in facs and "month" not in facs   # 被 _IC_SKIP 排除
    assert rep["buy"]["n"] == 50                          # bad_risk 未計入
