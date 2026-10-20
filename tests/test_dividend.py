"""股利政策摘要與白話解讀（2026-10-20）。

使用者要求完善個股研究的「股利政策（配息/殖利率）」。驗證：
  - _dividend_summary 由逐筆（可能季配）依年度加總、算出連續配息年數與近年平均，不跨期推估。
  - insights() 會把股利摘要翻成白話（連續配息穩定、最近配發、未配息）。
全部只用 FinMind 原始金額，抓不到就不產生對應洞見（不編造）。
"""
import finmind_data as fm


def _rows(pairs):
    # pairs: [(year, cash, stock), ...]；季配公司同一年多列
    return [{"period": y, "cash": c, "stock": s} for (y, c, s) in pairs]


def test_summary_aggregates_quarterly_into_year():
    # 2024 季配四次，各 1.0 → 全年現金 4.0；2023 一次 3.5
    rows = _rows([("2024", 1.0, 0), ("2024", 1.0, 0), ("2024", 1.0, 0), ("2024", 1.0, 0), ("2023", 3.5, 0)])
    s = fm._dividend_summary(rows)
    assert s["latest_year"] == "2024"
    assert s["latest_cash"] == 4.0
    assert s["consecutive_cash"] == 2
    assert s["avg_cash_recent"] == 3.75          # (4.0 + 3.5) / 2


def test_summary_consecutive_breaks_on_zero_cash():
    rows = _rows([("2021", 2.0, 0), ("2022", 0.0, 1.0), ("2023", 2.5, 0), ("2024", 3.0, 0)])
    s = fm._dividend_summary(rows)
    assert s["consecutive_cash"] == 2            # 2024、2023 連續，2022 現金為 0 中斷


def test_summary_empty_when_no_year():
    assert fm._dividend_summary([{"period": "", "cash": 1.0, "stock": 0}]) == {}


def test_insights_dividend_stable_and_latest():
    pack = {"dividend": {"ok": True, "summary": {
        "latest_year": "2025", "latest_cash": 16.0, "latest_stock": 0.0,
        "consecutive_cash": 7, "avg_cash_recent": 13.5, "avg_years": 5, "years_window": 8}}}
    texts = [i["text"] for i in fm.insights(pack)]
    assert any("最近配發年度 2025" in t and "16.0 元" in t for t in texts)
    assert any("連續 7 年配發現金股利" in t and "存股" in t for t in texts)


def test_insights_dividend_none_when_no_cash():
    pack = {"dividend": {"ok": True, "summary": {
        "latest_year": "2024", "latest_cash": 0.0, "latest_stock": 0.0,
        "consecutive_cash": 0, "avg_cash_recent": None, "avg_years": 0, "years_window": 3}}}
    texts = [i["text"] for i in fm.insights(pack)]
    assert any("未配發現金股利" in t for t in texts)
