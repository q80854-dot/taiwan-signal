"""臺灣 50 歷史成分股（point-in-time）與回測標的池（Q-P0-04，2026-11-12）。

原本回測的「TW50」是 stock_universe 裡一份寫死的清單：與真實成分差 18 檔、不隨時間變動。
改用使用者提供的 2020-01-01～2026-11-12 異動紀錄（88 筆）重建每日成分；驗收條件取自該研究報告。
本測試只能證明資料內部一致，不能證明與官方逐日名單完全相同（尚未以付費每日成分檔核驗）。
"""
import datetime as dt

import backtester as bt
import stock_universe
import tw50_history as h


def test_data_is_internally_consistent():
    assert h.validate() == []
    ev = h.events()
    assert len(ev) == 88 and sum(e["action"] == "ADD" for e in ev) == 44 and sum(e["action"] == "DELETE" for e in ev) == 44
    assert len(set(h.BASELINE)) == 50


def test_member_counts_including_delayed_inclusions():
    for day, n in (("2019-12-31", 50), ("2025-12-21", 50), ("2025-12-22", 49), ("2025-12-30", 49),
                   ("2025-12-31", 50), ("2026-03-22", 50), ("2026-03-23", 49), ("2026-04-13", 49),
                   ("2026-04-14", 50), ("2026-11-12", 50)):
        assert len(h.members_on(day)) == n, day


def test_announcement_is_not_membership():
    """公告日到生效日之間不得視為正式成分（否則是前視偏誤）；延後納入以實際生效日為準。"""
    assert h.is_member("6669", "2020-06-19") is False and h.is_member("6669", "2020-06-22") is True
    assert h.is_member("9904", "2020-06-19") is True and h.is_member("9904", "2020-06-22") is False
    assert h.is_member("2408.TW", "2025-12-22") is False and h.is_member("2408.TW", "2025-12-31") is True   # 南亞科
    assert h.is_member("3037", "2026-03-23") is False and h.is_member("3037", "2026-04-14") is True         # 欣興
    pend = [e["code"] for e in h.announced_pending("2026-03-30")]
    assert pend == ["3037"]                                    # 已公告、尚未生效：另列，不混入成分


def test_reentries_keep_full_history_and_unknown_period_is_not_guessed():
    assert h.is_member("1402", "2021-12-19") is True and h.is_member("1402", "2022-06-01") is False
    assert h.is_member("1402", "2023-01-03") is True and h.is_member("1402", "2023-09-18") is False        # 遠東新二進二出
    assert h.members_on("2019-12-30") is None and h.is_member("2330", "2015-01-05") is None                # 基準日前：不知道
    assert "2823" in h.members_between("2020-01-01", "2026-11-12")                                         # 已下市者仍在歷史標的池
    assert "2823" not in h.members_on("2026-11-12")


def test_no_unpublished_future_review_is_included():
    assert max(e["announcement_date"] for e in h.events()) <= h.VERIFIED_THROUGH
    assert max(e["actual_effective_date"] for e in h.events()) == "2026-09-21"


def test_current_component_list_now_comes_from_history():
    cur = stock_universe.get_tw50_components()
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d")
    assert set(cur) == set(h.tickers(h.members_on(today))) and len(cur) in (49, 50)
    assert all(t.endswith(".TW") for t in cur)


def test_default_backtest_universe_is_tagged_point_in_time():
    t = bt._tw50_targets(start="2022-01-01", end="2022-12-31")
    assert getattr(t, "pit", None) == "tw50"
    codes = {x.split(".")[0] for x in t}
    assert {"8454", "6770", "4938", "2409"} <= codes            # 2022 年內進出過的都要載入
    assert "3661" not in codes                                  # 2024 才納入：2022 回測不該出現
    assert bt._pit_ok("tw50", "6770.TW", "2022-06-20") and not bt._pit_ok("tw50", "6770.TW", "2022-09-19")
    assert bt._pit_ok(None, "9999.TW", "2022-06-20")            # 自訂清單不檢查
    assert not bt._pit_ok("tw50", "2330.TW", None)              # 缺日期：無法確認就不進場
    assert bt._pit_label("tw50").startswith("TW50_PIT") and bt._pit_label(None).startswith("PIT_UNAVAILABLE")


def _dates(n, start):
    return [(start + dt.timedelta(days=i)).isoformat() for i in range(n)]


def test_portfolio_backtest_only_enters_while_ticker_is_a_member(monkeypatch):
    """6770 力積電 2022-06-20 納入、2022-09-19 剔除：訊號日在成分期間才進場，之後不得再進。"""
    import data_fetcher, signal_engine
    n = 200
    def data(start):
        return {"dates": _dates(n, start), "closes": [100.0] * n, "highs": [100.5] * n, "lows": [99.5] * n,
                "opens": [100.0] * n, "volumes": [10000] * n}
    in_period = data(dt.date(2022, 6, 30) - dt.timedelta(days=149))     # 第 150 根（訊號日 index 149）＝2022-06-30
    after = data(dt.date(2022, 10, 3) - dt.timedelta(days=149))         # 訊號日＝2022-10-03（已剔除）
    monkeypatch.setattr(stock_universe, "get_stock_info", lambda t: {"size_cat": "中型股", "volume_lots": 999999})

    def _mtf(tf, **k):
        if len(tf["daily"]["closes"]) == 150:
            return {"direction": "buy", "score": 90, "adx_value": 30, "vol_ratio": 2.0,
                    "entry_indicators": {"atr": {"value": 2.0}}}
        return {"direction": "none", "score": 0}
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw", _mtf)
    monkeypatch.setattr(signal_engine, "calc_stop_loss_tw", lambda d, p, a, ind, sc, lo, hi: round(p * 0.95, 2))
    monkeypatch.setattr(signal_engine, "calc_take_profits_tw",
                        lambda d, p, sl, sc: {"tp1": p * 1.05, "tp2": p * 1.10, "tp3": p * 1.15, "rr1": 2.0})
    monkeypatch.setattr(signal_engine, "calc_position_size",
                        lambda p, sl, balance=None, size_cat=None, avg_volume_lots=None: {"shares": 3000})
    targets = bt._PitTargets(["6770.TW"]); targets.pit = "tw50"

    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": in_period)
    out = bt.portfolio_backtest_tw(tickers=targets, min_score=65, max_positions=1)
    assert out["n_trades"] == 1 and out["trades"][0]["bar_in"] == "2022-06-30"
    assert out["universe_pit"].startswith("TW50_PIT") and "universe_no_data" in out

    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": after)
    assert bt.portfolio_backtest_tw(tickers=targets, min_score=65, max_positions=1)["n_trades"] == 0
    plain = bt.portfolio_backtest_tw(tickers=["6770.TW"], min_score=65, max_positions=1)
    assert plain["n_trades"] == 1 and plain["universe_pit"].startswith("PIT_UNAVAILABLE")   # 不檢查成分時會進場


def test_single_symbol_backtest_respects_membership(monkeypatch):
    import data_fetcher, signal_engine
    n = 200
    start = dt.date(2022, 10, 3) - dt.timedelta(days=149)
    d = {"dates": _dates(n, start), "closes": [100.0] * n, "highs": [100.5] * n, "lows": [99.5] * n,
         "opens": [100.0] * n, "volumes": [10000] * n}
    monkeypatch.setattr(stock_universe, "get_stock_info", lambda t: {"size_cat": "中型股", "volume_lots": 999999})
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": d if tf == "daily" else None)
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw",
                        lambda tf, **k: ({"direction": "buy", "score": 90, "adx_value": 30, "vol_ratio": 2.0,
                                          "entry_indicators": {"atr": {"value": 2.0}}}
                                         if len(tf["daily"]["closes"]) == 150 else {"direction": "none", "score": 0}))
    monkeypatch.setattr(signal_engine, "calc_stop_loss_tw", lambda d_, p, a, ind, sc, lo, hi: round(p * 0.95, 2))
    monkeypatch.setattr(signal_engine, "calc_take_profits_tw",
                        lambda d_, p, sl, sc: {"tp1": p * 1.05, "tp2": p * 1.10, "tp3": p * 1.15, "rr1": 2.0})
    monkeypatch.setattr(signal_engine, "calc_position_size",
                        lambda p, sl, balance=None, size_cat=None, avg_volume_lots=None: {"shares": 3000})
    assert bt.backtest_symbol_tw("6770.TW", use_short_gates=False, pit_universe="tw50")["n_trades"] == 0
    assert bt.backtest_symbol_tw("6770.TW", use_short_gates=False)["n_trades"] == 1
    assert bt.backtest_symbol_tw_partial("6770.TW", pit_universe="tw50")["n_trades"] == 0
    assert bt.backtest_symbol_tw_partial("6770.TW")["n_trades"] == 1
