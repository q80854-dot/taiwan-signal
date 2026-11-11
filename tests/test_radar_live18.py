"""LIVE18（第三階段審查）：雷達「合理價成長」命名正名。

PE ÷ 單月營收年增 < 1 是機械篩選公式，不能支持「合理價」判斷；改為明確公式命名並揭露限制
（用單月營收年增而非 EPS 成長、易受低基期/一次性營收影響、非推薦標籤）。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_garp_filter_renamed_and_disclosed():
    s = _dash()
    # garp 預設仍存在，但不再用「合理價成長」這個暗示合理價判斷的名稱
    assert "'garp'" in s
    assert "合理價成長" not in s
    # 明確公式命名＋限制揭露
    assert "PE低於營收年增" in s
    assert "非合理價或推薦標籤" in s
    assert "一次性營收" in s
