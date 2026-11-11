"""LIVE09（第三階段審查）：個股頁訊號狀態關聯。

active_signal 僅指「今日新掃描訊號」；當它為空、但此股仍有未結案的既有訊號在追蹤時，
須標示 tracked，不可一律顯示「無有效訊號」而與事件/持倉頁矛盾。
"""
import app


def test_tracked_when_pending_history_and_no_active():
    hist = [
        {"id": "a", "status": "active", "result": "pending", "generated_at": "2026-11-10"},
        {"id": "b", "status": "closed", "result": "tp1", "generated_at": "2026-10-01"},
    ]
    assert app._pick_tracked_signal(hist, None)["id"] == "a"


def test_no_tracked_when_all_closed():
    hist = [{"id": "b", "status": "closed", "result": "sl", "generated_at": "2026-10-01"}]
    assert app._pick_tracked_signal(hist, None) is None


def test_active_itself_not_double_marked():
    hist = [{"id": "a", "status": "active", "result": "pending", "generated_at": "2026-11-11"}]
    # 今日新訊號就是這筆，不再重覆標為 tracked
    assert app._pick_tracked_signal(hist, {"id": "a"}) is None


def test_tracked_distinct_from_active():
    hist = [
        {"id": "today", "status": "active", "result": "pending", "generated_at": "2026-11-11"},
        {"id": "old", "status": "active", "result": "pending", "generated_at": "2026-11-05"},
    ]
    # active 是 today，tracked 應挑出最近的未結案——此處最近一筆即 today，與 active 同 → None
    assert app._pick_tracked_signal(hist, {"id": "today"}) is None
