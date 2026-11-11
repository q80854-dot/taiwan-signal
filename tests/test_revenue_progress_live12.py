"""LIVE12（第三階段審查）：月營收公布進度分子/分母正名。

n_expected＝已公布本期家數（分子）、n_total＝全市場總數（分母）。各頁一律 received/universe，
進度 = received/universe，夾 0~100%，不得再出現「1988/1849 已公布」這種顛倒、>100% 的顯示。
"""
import fundamentals as fd


def test_revenue_status_alias_fields(monkeypatch):
    monkeypatch.setitem(fd._rev_state, "n_expected", 1849)
    monkeypatch.setitem(fd._rev_state, "n_total", 1988)
    st = fd.revenue_status()
    # 別名語意明確：received＝已公布(分子)、universe＝全市場(分母)
    assert st["received_for_period"] == 1849 == st["n_expected"]
    assert st["universe_total"] == 1988 == st["n_total"]
    # 分子 ≤ 分母（已公布不可超過母體）
    assert st["received_for_period"] <= st["universe_total"]
    pct = round(st["received_for_period"] / st["universe_total"] * 100)
    assert pct == 93 and pct <= 100
