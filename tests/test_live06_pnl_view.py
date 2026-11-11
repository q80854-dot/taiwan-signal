"""LIVE06（第三階段審查）：事件與收益分欄、缺報價不 fallback 0%。

pending/追蹤中訊號沒有已實現損益，資料表 DEFAULT 0 不可冒充『0.0% 報酬』→ 一律 None。
已結案附 result_event 與 profit_flag（達目標不等於淨獲利）。
"""
import app


def test_pending_has_no_realized_pnl():
    r = app._live06_pnl_view({"status": "active", "result": "pending",
                              "pnl_pct": 0, "pnl_twd": 0, "close_price": None})
    assert r["pnl_pct"] is None and r["pnl_twd"] is None
    assert r["profit_flag"] is None
    assert r["result_event"] == "pending"


def test_closed_tp1_with_net_loss_profit_flag_false():
    # 達目標1 但淨損 → profit_flag=False（事件≠獲利）
    r = app._live06_pnl_view({"status": "closed", "result": "tp1",
                              "pnl_pct": -0.8, "pnl_twd": -1123})
    assert r["result_event"] == "tp1"
    assert r["profit_flag"] is False
    assert r["pnl_twd"] == -1123


def test_closed_tp_with_net_gain_profit_flag_true():
    r = app._live06_pnl_view({"status": "closed", "result": "tp2",
                              "pnl_pct": 2.1, "pnl_twd": 3200})
    assert r["profit_flag"] is True


def test_closed_missing_pnl_flag_none():
    r = app._live06_pnl_view({"status": "closed", "result": "expired",
                              "pnl_pct": None, "pnl_twd": None})
    assert r["profit_flag"] is None
