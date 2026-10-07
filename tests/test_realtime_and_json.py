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
