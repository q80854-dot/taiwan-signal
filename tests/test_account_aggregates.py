"""LIVE02（第三階段審查）：帳戶概況口徑拆分。

持倉頁原本把空單名目加進「持股市值」、並以「帳戶資金−含空單名目的成本」當可用現金。
_account_aggregates 應把多單資產與空單名目分開：持股市值只計多單、可用現金只扣多單成本、
未實現損益與風險依方向正確。
"""
import app as app_module


def _v(direction, shares, entry, last, stop):
    return {"direction": direction, "shares": shares, "entry": entry, "last": last, "stop": stop}


def test_account_aggregates_separates_long_and_short():
    views = [
        _v("buy", 1000, 100, 110, 95),    # 多：市值110000、成本100000、浮盈+10000、風險(110-95)*1000=15000
        _v("buy", 500, 50, 48, 45),       # 多：市值24000、成本25000、浮虧-1000、風險(48-45)*500=1500
        _v("sell", 200, 700, 720, 740),   # 空：名目144000、進場名目140000、浮虧(700-720)*200=-4000、風險(740-720)*200=4000
    ]
    acc = app_module._account_aggregates(views, balance=500000)
    assert acc["long_market_value"] == 134000      # 只計多單，不含空單名目
    assert acc["short_notional"] == 144000         # 空單名目單列
    assert acc["long_cost"] == 125000
    assert acc["short_entry_notional"] == 140000
    assert acc["cash_available"] == 375000          # 500000 − 多單成本125000（空單名目不佔現金）
    assert acc["unrealized_pnl"] == 5000            # 10000 − 1000 − 4000
    assert acc["cur_risk"] == 20500                 # 15000 + 1500 + 4000
    assert acc["n_long"] == 2 and acc["n_short"] == 1


def test_account_aggregates_short_only_does_not_consume_cash():
    acc = app_module._account_aggregates([_v("sell", 100, 600, 610, 630)], balance=500000)
    assert acc["long_market_value"] == 0
    assert acc["short_notional"] == 61000
    assert acc["cash_available"] == 500000          # 純空單 → 現金不被名目佔用


def test_account_aggregates_no_balance():
    acc = app_module._account_aggregates([_v("buy", 100, 100, 100, 95)], balance=0)
    assert acc["cash_available"] is None
