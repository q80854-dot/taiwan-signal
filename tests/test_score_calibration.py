"""P1 分數↔淨R 校準測試（2026-11-06）：Spearman 等級相關、單調性判定、樣本不足揭露、
排除 bad_risk。確保『分數有沒有換到更好的淨R』能被誠實量化、不虛構。"""
import learning as L


# ── Spearman ──
def test_spearman_perfect_positive():
    xs = [1, 2, 3, 4, 5]
    ys = [10, 20, 30, 40, 50]
    assert abs(L._spearman(xs, ys) - 1.0) < 1e-9


def test_spearman_perfect_negative():
    xs = [1, 2, 3, 4, 5]
    ys = [5, 4, 3, 2, 1]
    assert abs(L._spearman(xs, ys) + 1.0) < 1e-9


def test_spearman_handles_ties_and_small_n():
    assert L._spearman([1, 1], [2, 3]) is None          # n<3
    assert L._spearman([1, 1, 1], [2, 3, 4]) is None     # x 零變異


# ── _calib_one 單調性與樣本不足 ──
def _rows(spec):
    """spec: list of (score, r_multiple, result)。"""
    out = []
    for i, (sc, rm, res) in enumerate(spec):
        out.append({"score": sc, "r_multiple": rm, "result": res,
                    "bar_date": f"2026-10-{(i % 25) + 1:02d}", "direction": "buy"})
    return out


def test_calib_insufficient_sample():
    out = L._calib_one(_rows([(80, 1.0, "tp1")] * 5), "做多")
    assert out["insufficient"] is True and out["n"] == 5


def test_calib_detects_non_monotonic():
    # 三個分桶、中桶最高、高桶最差 → 非單調，且整體分數越高淨R越差（負相關）
    spec = ([(66, 1.0, "tp1")] * 12 + [(77, 2.0, "tp1")] * 12 + [(88, -1.0, "sl")] * 12)
    out = L._calib_one(_rows(spec), "做多")
    assert out["n"] == 36
    assert out["monotonic"] is not None and "非單調" in out["monotonic"]
    assert out["spearman"] is not None and out["spearman"] < 0      # 分數越高淨R越差
    assert "負相關" in out["verdict"]


def test_calib_detects_monotonic_positive():
    spec = ([(66, -0.5, "sl")] * 12 + [(77, 0.3, "tp1")] * 12 + [(88, 1.5, "tp1")] * 12)
    out = L._calib_one(_rows(spec), "做多")
    assert out["spearman"] > 0
    assert out["monotonic"] is not None and "單調上升" in out["monotonic"]


# ── score_calibration 排除 bad_risk / 未結算 ──
def test_score_calibration_excludes_bad_risk(monkeypatch):
    rows = []
    # 20 筆正常已結算候選
    for i in range(20):
        rows.append({"kind": "candidate", "status": "closed", "r_multiple": 1.0, "result": "tp1",
                     "score": 80, "direction": "buy", "bar_date": f"2026-10-{(i % 25) + 1:02d}", "f": {}})
    # 髒列：bad_risk（應被排除）
    rows.append({"kind": "candidate", "status": "closed", "r_multiple": 9e9, "result": "bad_risk",
                 "score": 80, "direction": "buy", "bar_date": "2026-10-01", "f": {}})
    # 未結算（應被排除）
    rows.append({"kind": "candidate", "status": "pending", "r_multiple": None, "result": None,
                 "score": 80, "direction": "buy", "bar_date": "2026-10-01", "f": {}})
    monkeypatch.setattr(L, "_rows", lambda: rows)
    out = L.score_calibration()
    assert out["ok"] is True
    assert out["buy"]["n"] == 20          # 只算 20 筆正常；bad_risk 與 pending 被排除
