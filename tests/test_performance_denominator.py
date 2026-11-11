"""LIVE05（第三階段審查）：績效分母正名。

get_performance_summary 原本 closed=wins+losses（排除逾期）卻被標「已結案」，win_rate 分母靜默
排除逾期，且無「淨獲利」口徑。本測試驗證新增欄位語意正確、且與原欄位對照一致（不無聲改歷史）。
以 delta 方式驗證，避免受資料庫既有列影響。
"""
from state_store import store

_IDS = [f"PERFTEST{i}" for i in range(1, 8)]
# (id, code, direction, result, pnl)  — 6 筆一般 + 1 筆 ETF 放空（應被排除）
_ROWS = [
    ("PERFTEST1", "9001", "buy",  "tp1",     1000),   # 觸停利＋淨獲利
    ("PERFTEST2", "9002", "buy",  "tp2",      500),   # 觸停利＋淨獲利
    ("PERFTEST3", "9003", "buy",  "sl",      -800),   # 停損
    ("PERFTEST4", "9004", "buy",  "sl",      -600),   # 停損
    ("PERFTEST5", "9005", "buy",  "expired",  300),   # 逾期但淨獲利（非觸停利）
    ("PERFTEST6", "9006", "buy",  "expired", -200),   # 逾期且虧損
    ("PERFTEST7", "0056", "sell", "tp1",      999),   # ETF 放空 → 應被排除，不影響統計
]


def _cleanup():
    with store._conn() as conn:
        conn.execute("DELETE FROM signals WHERE id IN (%s)" % ",".join("?" * len(_IDS)), tuple(_IDS))


def _insert():
    with store._conn() as conn:
        for _id, code, direction, result, pnl in _ROWS:
            conn.execute(
                "INSERT INTO signals (id,ticker,code,direction,result,pnl_twd,status) "
                "VALUES (?,?,?,?,?,?,?)",
                (_id, code + ".TW", code, direction, result, pnl, "closed"))


def test_performance_denominators_named_and_consistent():
    _cleanup()
    before = store.get_performance_summary()
    try:
        _insert()
        after = store.get_performance_summary()
    finally:
        _cleanup()

    d = lambda k: (after.get(k, 0) or 0) - (before.get(k, 0) or 0)
    assert d("closed_total") == 6          # 6 筆一般終止交易（ETF 放空被排除）
    assert d("total") == 6                 # closed_total 與原 total 同義
    assert d("target_hit") == 2            # tp1/tp2
    assert d("decided") == 4               # 觸停利 2 ＋ 停損 2（排除 2 筆逾期）
    assert d("net_profitable") == 3        # +1000, +500, +300（含逾期的淨獲利）
    assert d("wins") == 2                  # 原欄位對照：wins == target_hit

    # 口徑不變量（與分母有關，不依賴其他列）
    assert after["target_hit_rate"] == after["win_rate"]          # 觸停利率＝原勝率（分母 decided）
    assert after["closed_total"] == after["total"]
    assert after["decided"] == after["wins"] + after["losses"]
    # net_profitable_rate 分母為 closed_total（含逾期），與 target_hit_rate 分母不同
    assert after["net_profitable_rate"] == round(
        after["net_profitable"] / max(after["closed_total"], 1) * 100, 1)
