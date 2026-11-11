"""SH11（第三階段審查）：推播送達對帳分母。

delivered=1 只代表平台至少一目標回 200（已確認）；其餘是 unknown，非一定失敗。
confirmed_rate 是已確認比例，不等於真實送達率。
"""
import shadow


def test_delivery_summary_unknown_is_not_failure():
    d = shadow._delivery_summary(sent=22, delivered=3)
    assert d["sent"] == 22 and d["delivered"] == 3
    assert d["unknown"] == 19                 # 未確認，非失敗
    assert d["confirmed_rate"] == round(3 / 22 * 100, 1)   # 已確認比例（13.6%）
    assert "非真實送達率" in d["note"] and "unknown" in d["note"]


def test_delivery_summary_empty():
    d = shadow._delivery_summary(sent=0, delivered=0)
    assert d["confirmed_rate"] is None and d["unknown"] == 0


def test_delivery_summary_all_confirmed():
    d = shadow._delivery_summary(sent=5, delivered=5)
    assert d["unknown"] == 0 and d["confirmed_rate"] == 100.0
