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


def test_financial_industry_skips_inapplicable(monkeypatch):
    ind = _ind()
    ind["industry"] = "金融保險"
    ind["metrics"].append({"key": "current_ratio", "rows": _rows([50] * 8)})
    monkeypatch.setattr(fm, "sec_indicators", lambda c: ind)
    monkeypatch.setattr(fm, "sec_dividend", lambda c: {"ok": True, "records": []})
    q = fund_score.build_quality("2881")
    labels = [i["label"] for i in q["items"]]
    assert "負債比率" not in labels and "流動比率" not in labels
    assert q["notes"] and q["industry"] == "金融保險"


def test_construction_uses_looser_debt_threshold(monkeypatch):
    ind = _ind()
    ind["industry"] = "建材營造"
    ind["metrics"] = [m if m["key"] != "debt_ratio" else {"key": "debt_ratio", "rows": _rows([50] * 8)} for m in ind["metrics"]]
    monkeypatch.setattr(fm, "sec_indicators", lambda c: ind)
    monkeypatch.setattr(fm, "sec_dividend", lambda c: {"ok": True, "records": []})
    by = {i["label"]: i for i in fund_score.build_quality("2501")["items"]}
    assert by["負債比率"]["status"] == "good"      # 一般產業 50% 只算普通，營建 ≤55% 算優


def test_financial_statements_use_income_after_tax(monkeypatch):
    """金融保險業損益表科目是 IncomeAfterTax（沒有 s），且資產負債表沒有母公司權益科目 → 用 權益−非控制權益。"""
    def rows(d, kv):
        return [{"date": d, "type": k, "value": v} for k, v in kv.items()]
    inc = rows("2026-06-30", {"Revenue": 1000e8, "IncomeAfterTax": 100e8, "EquityAttributableToOwnersOfParent": 98e8, "EPS": 1.0})
    bs = rows("2026-06-30", {"TotalAssets": 10000e8, "Liabilities": 9000e8, "Equity": 1000e8, "NoncontrollingInterests": 20e8})

    def fake(dataset, *a, **k):
        r = {"TaiwanStockFinancialStatements": inc, "TaiwanStockBalanceSheet": bs}.get(dataset, [])
        return {"rows": r, "ok": True, "stale": False, "error": None}
    monkeypatch.setattr(fm, "fm_get", fake)
    out = fm.sec_indicators("2881")
    by = {m["key"]: m["rows"][-1]["value"] for m in out["metrics"]}
    assert by["net_income"] == 100.0
    assert by["roe"] == round(98 / 980 * 100, 2)
