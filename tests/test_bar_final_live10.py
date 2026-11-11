"""LIVE10（第三階段審查）：未收盤日K不得標「收盤」（顯示層）。

盤中（info.live）時：研究摘要不得標「{日期} 收盤」而須標「盤中（暫定，未收盤）」；
關鍵價位註記含今日暫定K棒；估值（收盤後資料）與盤中價格非同一時點須逐欄說明。
完整 bar.is_final/session/source_time 資料契約（逐棒/逐欄）需後端 schema，列為缺口。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_summary_marks_provisional_when_live():
    s = _dash()
    assert "盤中（暫定，未收盤" in s
    # 收盤標示仍存在（非盤中時）
    assert "：'收盤'}" in s or ":'收盤'}" in s


def test_key_levels_note_provisional_bar():
    s = _dash()
    assert s.count("含今日暫定K棒") >= 2        # 系統評分卡＋技術分頁關鍵價位


def test_valuation_not_same_session_note():
    s = _dash()
    assert "官方收盤後估值" in s and "非盤中同一時點" in s
