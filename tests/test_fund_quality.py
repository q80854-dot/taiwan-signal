import fund_score
import finmind_data as fm


def _rows(vals, start=2024):
    out = []
    for i, v in enumerate(vals):
        y, q = start + i // 4, i % 4 + 1
        out.append({"period": f"{y}/Q{q}", "date": f"{y}-{q * 3:02d}-00", "value": v, "yoy": None, "yoy_pp": None})
    return out


def _ind():
    eps = _rows([2, 2, 2, 2, 3, 3, 3, 3])
    for i in range(4, 8):
        eps[i]["yoy"] = 50.0
    return {"ok": True, "stale": False, "date": "2025-12-31", "metrics": [
        {"key": "eps", "rows": eps},
        {"key": "roe", "rows": _rows([5] * 8)},
        {"key": "ocf", "rows": _rows([10] * 8)},
        {"key": "net_income", "rows": _rows([8] * 8)},
        {"key": "debt_ratio", "rows": _rows([30] * 8)},
    ]}


def test_build_quality_scores_items(monkeypatch):
    monkeypatch.setattr(fm, "sec_indicators", lambda c: _ind())
    monkeypatch.setattr(fm, "sec_dividend", lambda c: {"ok": True, "records": []})
    q = fund_score.build_quality("2330")
    assert q["ok"] and q["n"] >= 4
    by = {i["label"]: i for i in q["items"]}
    assert by["ROE（近四季合計）"]["status"] == "good"          # 5*4 = 20% >= 15%
    assert by["負債比率"]["status"] == "good"
    assert by["營業現金流 ÷ 淨利（近四季）"]["status"] == "good"  # 125%
    assert by["EPS 連續同比成長"]["value"] == "4 季"
    assert 0 <= q["score"] <= 100


def test_build_quality_no_data(monkeypatch):
    monkeypatch.setattr(fm, "sec_indicators", lambda c: {"ok": False, "error": "x", "metrics": []})
    q = fund_score.build_quality("9999")
    assert q["ok"] is False
