"""SH08（第三階段審查）：策略學習頁的累積 R 曲線正名。

learning._equity_curve 是「按決策日(訊號日)歸因」的累積淨 R，非實際帳戶資金曲線。
build_report 應附 equity_curve_note 誠實說明，避免被當成可成交帳戶權益。
"""
import learning as L


def test_build_report_has_equity_curve_note(monkeypatch):
    monkeypatch.setattr(L, "_rows", lambda: [])          # 空樣本也要有誠實註記
    rep = L.build_report()
    assert "equity_curve_note" in rep
    assert "決策日" in rep["equity_curve_note"]
    assert "非帳戶資金曲線" in rep["equity_curve_note"]


def test_equity_curve_attributes_by_signal_date():
    # 兩筆已結算候選，累積 R 依 bar_date 排序累加（決策日歸因）。
    rows = [{"status": "closed", "r_multiple": 1.0, "bar_date": "2026-10-02", "id": 1},
            {"status": "closed", "r_multiple": -0.5, "bar_date": "2026-10-03", "id": 2}]
    pts = L._equity_curve(rows)
    assert [p["date"] for p in pts] == ["2026-10-02", "2026-10-03"]
    assert pts[-1]["cum_r"] == 0.5
