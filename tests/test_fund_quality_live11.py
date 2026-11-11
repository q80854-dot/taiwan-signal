"""LIVE11（第三階段審查）：財報體質體檢四態（pass/warn/fail/unknown/not_applicable）。

缺資料(unknown)與不適用(na) 不計分、不算「未通過」，並揭露有效分母與覆蓋信心。
"""
import fund_score as F


def _ind(industry, metrics):
    return {"ok": True, "industry": industry, "date": "2026-11-01", "stale": False, "metrics": metrics}


def _patch(monkeypatch, ind):
    import finmind_data as fm
    monkeypatch.setattr(fm, "sec_indicators", lambda code: ind)
    monkeypatch.setattr(fm, "sec_dividend", lambda code: {"ok": False, "records": []}, raising=False)


def test_financial_company_marks_na_not_fail(monkeypatch):
    roe = [{"value": 4.0, "period": f"2026Q{i}"} for i in range(1, 5)]
    _patch(monkeypatch, _ind("金融保險業", [{"key": "roe", "rows": roe}]))
    q = F.build_quality("2881")
    cov = q["coverage"]
    # 毛利率/營業利益率對金融業＝na、不計分
    nas = [i for i in q["items"] if i["status"] == "na"]
    assert any("毛利率" in i["label"] for i in nas)
    assert any("營業利益率" in i["label"] for i in nas)
    assert cov["not_applicable"] >= 2
    # 分數只由有效項（ROE）計算，不被 na 拉低
    assert q["n"] == cov["evaluated"] >= 1


def test_missing_data_is_unknown_not_fail(monkeypatch):
    roe = [{"value": 20.0, "period": f"2026Q{i}"} for i in range(1, 5)]
    _patch(monkeypatch, _ind("半導體", [{"key": "roe", "rows": roe}]))  # 無 gross/op 資料
    q = F.build_quality("2330")
    unknowns = [i for i in q["items"] if i["status"] == "unknown"]
    assert any("毛利率" in i["label"] for i in unknowns)
    assert any("營業利益率" in i["label"] for i in unknowns)
    assert q["coverage"]["unknown"] >= 2
    # unknown 不計分：score 僅由 ROE（good）決定 = 100
    assert q["score"] == 100
    assert q["coverage"]["confidence"] in ("low", "medium", "high")


def test_low_coverage_flag(monkeypatch):
    roe = [{"value": 20.0, "period": f"2026Q{i}"} for i in range(1, 5)]
    _patch(monkeypatch, _ind("半導體", [{"key": "roe", "rows": roe}]))
    q = F.build_quality("2330")
    # 只有 1 項有效評分 → 低覆蓋、結論暫定
    assert q["low_coverage"] is True
