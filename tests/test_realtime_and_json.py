import json
import time
import pytest
import app as app_module
import realtime
import stock_universe as su
from datetime import datetime
import pytz

TZ = pytz.timezone("Asia/Taipei")


def test_json_nan_becomes_null():
    with app_module.app.test_request_context():
        from flask import jsonify
        body = jsonify({"a": float("nan"), "b": [1.0, float("inf"), float("-inf")], "c": {"d": float("nan")}}).get_data(as_text=True)
    j = json.loads(body)                       # 標準 JSON 解析器必須能讀（以前 NaN 會失敗）
    assert j == {"a": None, "b": [1.0, None, None], "c": {"d": None}}


def test_json_numpy_values():
    np = pytest.importorskip("numpy")
    with app_module.app.test_request_context():
        from flask import jsonify
        body = jsonify({"x": np.float32("nan"), "y": np.float64(1.5), "z": np.array([1.0, np.nan])}).get_data(as_text=True)
    assert json.loads(body) == {"x": None, "y": 1.5, "z": [1.0, None]}


def test_parse_mis_row_normal():
    q = realtime.parse_mis_row({"c": "00631L", "n": "元大台灣50正2", "ex": "tse", "z": "41.69", "y": "39.58",
                                "o": "41.5", "h": "41.9", "l": "41.4", "v": "57327", "t": "11:20:03", "d": "20261007"})
    assert q["price"] == 41.69 and q["prev_close"] == 39.58 and q["date"] == "2026-10-07"
    assert q["change"] == 2.11 and q["change_pct"] == 5.33 and q["volume_lots"] == 57327 and q["market"] == "TSE"


def test_parse_mis_row_no_trade_does_not_guess():
    q = realtime.parse_mis_row({"c": "2330", "z": "-", "y": "2500.0000", "d": "20261007", "t": "08:59:00"})
    assert q["price"] is None and q["change"] is None and q["change_pct"] is None


def test_ex_ch_market_mapping():
    assert realtime._ex_ch("2330", "TSE") == ["tse_2330.tw"]
    assert realtime._ex_ch("6182", "OTC") == ["otc_6182.tw"]
    assert realtime._ex_ch("1234", None) == ["tse_1234.tw", "otc_1234.tw"]


def test_market_open_hours():
    assert realtime.market_open(TZ.localize(datetime(2026, 10, 7, 11, 20)))      # 週三盤中
    assert not realtime.market_open(TZ.localize(datetime(2026, 10, 7, 8, 59)))
    assert not realtime.market_open(TZ.localize(datetime(2026, 10, 7, 14, 0)))
    assert not realtime.market_open(TZ.localize(datetime(2026, 10, 10, 11, 0)))  # 週六
    # 國定假日（平日）即使在交易時段也不是盤中——2026-10-09 國慶日補假（config.TW_MARKET_HOLIDAYS）
    assert not realtime.market_open(TZ.localize(datetime(2026, 10, 9, 11, 0)))


def test_expected_latest_date():
    assert su._expected_latest_date(TZ.localize(datetime(2026, 10, 7, 11, 20))) == "2026-10-06"   # 盤中：前一個交易日
    assert su._expected_latest_date(TZ.localize(datetime(2026, 10, 7, 16, 0))) == "2026-10-07"    # 收盤後：今天
    assert su._expected_latest_date(TZ.localize(datetime(2026, 10, 5, 10, 0))) == "2026-10-02"    # 週一：上週五
    assert su._expected_latest_date(TZ.localize(datetime(2026, 10, 10, 12, 0))) == "2026-10-09"    # 週六


def test_date_stale_logic():
    now = TZ.localize(datetime(2026, 10, 7, 16, 40))
    old = {"tse_quote_date": "2026-10-06", "otc_quote_date": "2026-10-06", "fetched_at": time.time() - 4000}
    assert su._date_stale(old, now)                                       # 收盤後仍是昨天、超過 30 分鐘 → 過期
    recent = dict(old, fetched_at=time.time() - 60)
    assert not su._date_stale(recent, now)                                # 剛抓過，先不重抓（避免狂打官方 API）
    fresh = dict(old, tse_quote_date="2026-10-07", otc_quote_date="2026-10-07")
    assert not su._date_stale(fresh, now)
    one_side = dict(old, tse_quote_date="2026-10-07")                     # 只有上市更新、上櫃還舊 → 仍過期
    assert su._date_stale(one_side, now)
    assert not su._date_stale({"fetched_at": 0}, now)                     # 沒有日期資訊 → 維持原行為


def test_parse_mis_row_uses_last_trade_when_current_tick_empty():
    # 實際從 Render 對 MIS 抓到的格式：z 是 "-"（這一盤沒成交），最近一筆成交在 trade.z
    q = realtime.parse_mis_row({"c": "2330", "ex": "tse", "z": "-", "y": "2585.0000", "d": "20261007", "t": "11:36:34",
                                "v": "8628", "trade": {"ft": 20, "t": "11:35:35", "v": 1, "z": "2575.0000"}})
    assert q["price"] == 2575.0 and q["time"] == "11:35:35" and q["change"] == -10.0 and q["change_pct"] == -0.39


def test_breadth_and_fresh_prices(monkeypatch):
    import realtime
    q = {"A": {"date": "2026-10-07", "price": 11.0, "prev_close": 10.0, "limit_up": 11.0, "limit_down": 9.0},
         "B": {"date": "2026-10-07", "price": 9.0, "prev_close": 10.0, "limit_up": 11.0, "limit_down": 9.0},
         "C": {"date": "2026-10-07", "price": 10.0, "prev_close": 10.0},
         "D": {"date": "2026-10-07", "price": None, "prev_close": 10.0},
         "E": {"date": "2026-10-06", "price": 10.0, "prev_close": 10.0}}
    b = realtime.compute_breadth(q, "2026-10-07")
    assert (b["up"], b["down"], b["flat"], b["limit_up"], b["limit_down"], b["no_trade"], b["stale"]) == (1, 1, 1, 1, 1, 1, 1)

    today = realtime.now_tpe().strftime("%Y-%m-%d")
    monkeypatch.setattr(realtime, "fetch_mis", lambda pairs: {
        "1101": {"price": 50.0, "date": today}, "1102": {"price": 40.0, "date": "2000-01-01"},
        "1103": {"price": None, "date": today}})
    monkeypatch.setattr(realtime, "fetch_fugle", lambda c: None)
    r = realtime.fresh_prices([("1101.TW", None), ("1102.TW", None), ("1103.TW", None), ("1104.TW", None)])
    assert r["prices"] == {"1101": 50.0}
    assert r["reasons"] == {"1102": "stale", "1103": "no_trade", "1104": "source_missing"}


def test_live_universe_overlay(monkeypatch):
    import stock_universe as su, realtime, time
    today = realtime.now_tpe().strftime("%Y-%m-%d")
    base = [{"code": "1101", "close": 40.0, "change_pct": 1.0, "volume_lots": 5000, "quote_date": "2000-01-01"},
            {"code": "1102", "close": 30.0, "change_pct": 2.0, "volume_lots": 100, "quote_date": "2000-01-01"},
            {"code": "1103", "close": 20.0, "change_pct": 3.0, "volume_lots": 100, "quote_date": today},
            {"code": "1104", "close": 10.0, "change_pct": 4.0, "volume_lots": 100, "quote_date": "2000-01-01"}]
    monkeypatch.setattr(su, "build_full_universe", lambda force_refresh=False: base)
    snap = {"at": time.time(), "quotes": {"1101": [44.0, 40.0, 9000, "10:00:00", today, None, None],
                                          "1102": [None, 30.0, None, None, today, None, None],
                                          "1103": [99.0, 20.0, 1, "10:00:00", today, None, None]}}
    monkeypatch.setattr(su, "_get_live_snapshot", lambda: snap)
    out = {s["code"]: s for s in su.build_live_universe()}
    assert out["1101"]["close"] == 44.0 and out["1101"]["change_pct"] == 10.0 and out["1101"]["volume_lots"] == 9000 and out["1101"]["live"]
    assert out["1102"]["change_pct"] is None and out["1102"]["volume_lots"] == 0       # 今日無成交，不沿用昨日漲跌幅
    assert out["1103"]["close"] == 20.0 and "live" not in out["1103"]                    # 官方日期已是今天，不覆蓋
    assert out["1104"]["close"] == 10.0                                                  # 不在快照內，維持官方資料
    monkeypatch.setattr(su, "_get_live_snapshot", lambda: None)
    assert su.build_live_universe()[0]["close"] == 40.0                                  # 快照過期 → 退回官方


def test_quality_check():
    import realtime
    off = [{"code": "A", "close": 10.0, "quote_date": "2000-01-01"}, {"code": "B", "close": 10.0, "quote_date": "2000-01-01"},
           {"code": "C", "close": 10.0, "quote_date": "2000-01-01"}, {"code": "D", "close": 5.0, "quote_date": "2000-01-01"}]
    q = {"A": [10.5, 10.0, 1, "t", "d", 11.0, 9.0], "B": [9.0, 9.5, 1, "t", "d", 11.0, 9.0],
         "C": [12.0, 10.0, 1, "t", "d", 11.0, 9.0]}
    r = realtime.quality_check(q, off, "2026-10-07")
    assert r["checked"] == 3 and r["prev_close_mismatch"] == 1 and r["out_of_limit"] == 1 and r["missing"] == 1


def test_official_bar_selection(monkeypatch):
    import stock_universe as su
    st = {"code": "2330", "quote_date": "2026-10-06", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume_lots": 5000}
    bad = {"code": "9999", "quote_date": "2026-10-06", "open": 100.0, "high": 90.0, "low": 99.0, "close": 104.0, "volume_lots": 1}
    monkeypatch.setattr(su, "_official_index", lambda: {"2330": st, "9999": bad})
    monkeypatch.setattr(su, "_mis_final_bars", lambda: None)
    b = su.get_official_bar("2330")
    assert b["source"] == "OpenAPI" and b["close"] == 104.0 and b["volume"] == 5000
    assert su.get_official_bar("9999") is None                       # 高低價不合理 → 不採用
    monkeypatch.setattr(su, "_mis_final_bars", lambda: {"date": "2026-10-07", "bars": {"2330": [104.0, 108.0, 103.0, 107.0, 6000]}})
    b = su.get_official_bar("2330")
    assert b["source"] == "MIS" and b["date"] == "2026-10-07" and b["close"] == 107.0   # OpenAPI 還沒更新到今天 → 用 MIS 定案


def test_apply_official_bar_appends_and_replaces(monkeypatch):
    import data_fetcher as df, stock_universe as su
    from state_store import store
    saved = []
    monkeypatch.setattr(su, "get_official_bar", lambda c: {"date": "2026-10-07", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                                                          "volume": 100, "source": "MIS"})
    monkeypatch.setattr(store, "get_cached_ohlcv_bars", lambda t, tf, limit=300: [{"bar_date": "2026-10-06", "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 90}])
    monkeypatch.setattr(store, "upsert_ohlcv_bars", lambda t, tf, bars: saved.append(bars))
    before = dict(df._obar_stats)
    df._apply_official_bar("1234.TW")
    assert saved and saved[0][0]["date"] == "2026-10-07" and saved[0][0]["close"] == 10.5
    assert df._obar_stats["appended"] == before["appended"] + 1
    # 同日、Yahoo 收盤差很多 → 取代並記為不一致
    saved.clear()
    monkeypatch.setattr(store, "get_cached_ohlcv_bars", lambda t, tf, limit=300: [{"bar_date": "2026-10-07", "open": 10, "high": 11, "low": 9.5, "close": 12.0, "volume": 100}])
    df._apply_official_bar("1234.TW")
    assert saved and saved[0][0]["close"] == 10.5 and df._obar_stats["mismatch"] >= 1
    # 完全相同 → 不重複寫入
    saved.clear()
    monkeypatch.setattr(store, "get_cached_ohlcv_bars", lambda t, tf, limit=300: [{"bar_date": "2026-10-07", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 100}])
    df._apply_official_bar("1234.TW")
    assert not saved


def test_snap_is_final_after_close_same_day():
    import stock_universe as su
    from datetime import datetime, timedelta
    def ts(y, m, d, hh, mm):   # 台北時間 → epoch
        return (datetime(y, m, d, hh, mm) - timedelta(hours=8) - datetime(1970, 1, 1)).total_seconds()
    assert su.snap_is_final({"at": ts(2026, 10, 7, 13, 35)}, ts(2026, 10, 7, 15, 10))
    assert not su.snap_is_final({"at": ts(2026, 10, 7, 13, 0)}, ts(2026, 10, 7, 15, 10))      # 盤中拍的不算最終
    assert not su.snap_is_final({"at": ts(2026, 10, 7, 13, 35)}, ts(2026, 10, 8, 9, 5))        # 隔天作廢
