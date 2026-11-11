"""SH04（第三階段審查）：unresolved 原因拆分，不再一律標「退市/停牌」。

離線無法區分退市/停牌/資料源中斷，故只按程式實際寫入的 result（nodata/bad_risk）誠實拆分。
"""
import learning as L


def test_unresolved_breakdown_splits_reasons():
    rows = [
        {"status": "unresolved", "result": "nodata"},
        {"status": "unresolved", "result": "nodata"},
        {"status": "unresolved", "result": "bad_risk"},
        {"status": "closed", "result": "tp1"},        # 已結算不計入
        {"status": "pending", "result": None},        # 追蹤中不計入
    ]
    ub = L._unresolved_breakdown(rows)
    assert ub == {"nodata": 2, "bad_risk": 1}


def test_unresolved_breakdown_unknown_bucket():
    rows = [{"status": "unresolved", "result": None},
            {"status": "unresolved"}]
    assert L._unresolved_breakdown(rows) == {"unknown": 2}


def test_unresolved_breakdown_empty():
    assert L._unresolved_breakdown([{"status": "closed", "result": "sl"}]) == {}
