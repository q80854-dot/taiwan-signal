"""LIVE07（第三階段審查）：批次混用誠實化（顯示層）。

漏斗（scan_audit＋scan_history 最新）與訊號卡（選定批次）可能非同一批：不同批時逐一明示；
0 訊號也說明原因（已掃完沒選出 vs 尚未掃描）並指向仍在追蹤的舊訊號；
總覽「今日訊號」卡顯示較早批次時，副標明示「{日期} 批次（最近有效訊號，非今日新發）」。
完整 scan_run_id 綁定需後端 schema，列為資料契約缺口。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_funnel_batch_mismatch_notes_present():
    s = _dash()
    assert "兩者非同一批" in s                       # 漏斗 vs 選定批次
    assert "與前三階段" in s and "非同一批" in s      # scan_audit vs scan_history
    assert "scan_run_id" in s                        # 契約缺口註記


def test_zero_signal_reason_present():
    s = _dash()
    assert "今日掃描已完成" in s
    assert "今日尚未掃描" in s
    assert "既有訊號仍持續追蹤" in s


def test_overview_batch_label_present():
    s = _dash()
    assert "最近有效訊號，非今日新發" in s
