"""站主專用法人張數原始診斷（get_inst_raw_debug，2026-10-21）。

使用者要徹底校對「法人張數」口徑（確認資料庫存的是『張』而非『股』）。
這裡驗證診斷方法：取最新一日、三大法人加總（自營商＝合計−外資−投信）、
前 N 檔依 |外資買賣超| 排序，單位沿用資料庫既有欄位、不做任何加工。
"""
from state_store import store


def _clear():
    with store._conn() as conn:
        conn.execute("DELETE FROM inst_daily")


def _day(d, **net):
    store.upsert_inst_daily(d, {c: {"name": c, "foreign_net": f, "trust_net": t, "total_net": f + t}
                                for c, (f, t) in net.items()})


def test_latest_day_aggregate_and_top():
    _clear()
    _day("2026-10-20", AAA=(100, 10), BBB=(-5, 0))          # 舊的一天不應出現在 agg
    _day("2026-10-21", AAA=(300, 20), BBB=(-800, 5), CCC=(50, 0))
    out = store.get_inst_raw_debug(top=2)
    assert out["date"] == "2026-10-21"                       # 只取最新一日
    assert out["rows"] == 3
    agg = out["agg"]
    assert agg["foreign"] == 300 - 800 + 50                  # -450
    assert agg["trust"] == 20 + 5 + 0                        # 25
    assert agg["total"] == (300 + 20) + (-800 + 5) + (50 + 0)  # 合計＝各股 total_net 相加
    assert agg["dealer"] == agg["total"] - agg["foreign"] - agg["trust"]
    # 前 2 檔依 |外資| 排序：BBB(800) > AAA(300)
    assert [r["code"] for r in out["top"]] == ["BBB", "AAA"]
    assert out["top"][0]["foreign"] == -800


def test_empty_returns_none_date():
    _clear()
    out = store.get_inst_raw_debug()
    assert out["date"] is None and out["rows"] == 0 and out["top"] == []
