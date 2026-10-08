"""2026-10-08 資料正確性修正的回歸測試：漲跌停判定、ETF 不列入家數、股→張四捨五入、缺資料時不給假分數。"""
import stock_universe as su


def test_limit_prices_follow_tick_rules():
    assert su.limit_prices(100) == (110.0, 90.0)
    assert su.limit_prices(99.9) == (109.5, 90.0)        # 109.89 → 0.5 檔位捨去
    assert su.limit_prices(9.99) == (10.95, 9.0)
    assert su.limit_prices(45.5) == (50.0, 40.95)        # 50.05 落在 0.1 檔位 → 50.0
    assert su.limit_prices(999) == (1095.0, 900.0)
    assert su.limit_prices(49.9, etf=True) == (54.85, 44.91)


def test_limit_state_not_fooled_by_big_moves():
    assert su.limit_state({"close": 109.5, "change": 9.6}) == 1      # 99.9 → 漲停 109.5
    assert su.limit_state({"close": 109.0, "change": 9.1}) == 0      # 漲 9.11% 但沒到漲停價
    assert su.limit_state({"close": 90.0, "change": -10.0}) == -1
    assert su.limit_state({"close": 10.5, "change": 0.5, "limit_up_px": 11.0, "limit_down_px": 9.0}) == 0
    assert su.limit_state({"close": 11.0, "limit_up_px": 11.0, "limit_down_px": 9.0}) == 1


def test_breadth_excludes_etf_and_uses_limit_prices(monkeypatch):
    uni = [
        {"code": "2330", "market": "TSE", "close": 109.5, "change": 9.6, "change_pct": 9.61, "is_etf": False},
        {"code": "2317", "market": "TSE", "close": 109.0, "change": 9.1, "change_pct": 9.11, "is_etf": False},
        {"code": "6488", "market": "OTC", "close": 90.0, "change": -10.0, "change_pct": -10.0, "is_etf": False},
        {"code": "0050", "market": "TSE", "close": 110.0, "change": 10.0, "change_pct": 10.0, "is_etf": True},
    ]
    monkeypatch.setattr(su, "build_live_universe", lambda: uni)
    b = su.get_market_breadth()
    assert (b["advancers"], b["decliners"], b["limit_up"], b["limit_down"]) == (2, 1, 1, 1)
    assert b["total"] == 3 and b["tse"]["advancers"] == 2


def test_compute_breadth_skips_etf():
    import realtime
    q = {"2330": {"date": "2026-10-07", "price": 11.0, "prev_close": 10.0},
         "0050": {"date": "2026-10-07", "price": 11.0, "prev_close": 10.0}}
    assert realtime.compute_breadth(q, "2026-10-07", skip={"0050"})["up"] == 1


def test_shares_to_lots_rounds_symmetrically():
    from data_fetcher import _lots
    assert _lots(1499) == 1 and _lots(1500) == 2 and _lots(-1999) == -2 and _lots(0) == 0 and _lots(-400) == 0


def test_env_score_is_none_when_index_missing(monkeypatch):
    import risk_manager as rm
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    st = rm.get_system_status({"index": {}, "foreign": {}})
    assert st["env_score"] is None and st["can_trade"] is False and st["env_status"] == "大盤資料異常"
    st = rm.get_system_status({"index": {"twii": {"chg": 0.2, "price": 20000}, "vix": {"price": 15}},
                               "foreign": {"net_buy_twd": 1e9}})
    assert isinstance(st["env_score"], int) and st["can_trade"]
