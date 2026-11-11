"""LIVE15（第三階段審查）：文件漂移——方法論門檻與實際設定對齊。

實際 config：做多 min_score=65、做空 min_score=70。方法論原本只寫「65 分」未區分多/空，
且未連結現行設定版本。修正後須同時列出做多 65／做空 70，並指向設定頁（config_version）。
"""
import os
from config import THRESH, SHORT_SIGNAL_THRESH


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_config_values_are_what_doc_states():
    assert THRESH.get("min_score") == 65
    assert SHORT_SIGNAL_THRESH.get("min_score") == 70


def test_methodology_distinguishes_long_short_and_links_version():
    s = _dash()
    # 方法論段落須同時說明做多 65 / 做空 70，並指向設定頁 config_version
    assert "做多</b>總分達 65 分" in s
    assert "需 ≥70 分" in s
    assert "config_version" in s
    assert "歷史訊號依其產生當時的參數判讀" in s
