"""逐棒資料契約階段 1（LIVE10 缺口，2026-11-12 使用者核准「按上面方案進行」）。

ohlcv_bars 每根 K 棒記血緣四欄：is_final（1=定案、0=暫定、NULL=舊列無紀錄不回填）、
session（regular/intraday）、source（yfinance/官方/finmind＝最後寫入來源）、
source_time（該來源取得時點）。寫入端三處全帶值；讀取端本階段不動（階段 2）。
"""
from datetime import datetime

import pandas as pd

import data_fetcher as df
from state_store import store

_TK = "BARSCHEMA_TEST.TW"


def _cleanup():
    with store._conn() as conn:
        conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (_TK,))


def _select_all():
    with store._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM ohlcv_bars WHERE ticker=? ORDER BY bar_date", (_TK,)).fetchall()]
    return rows


def test_upsert_roundtrip_and_null_when_absent():
    _cleanup()
    try:
        store.upsert_ohlcv_bars(_TK, "daily", [
            {"date": "2026-10-01", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
             "is_final": 1, "session": "regular", "source": "yfinance", "source_time": "2026-10-01T15:00:00"},
            {"date": "2026-10-02", "open": 1, "high": 2, "low": 0.5, "close": 1.6, "volume": 11},  # 未帶 → NULL
        ])
        r1, r2 = _select_all()
        assert r1["is_final"] == 1 and r1["source"] == "yfinance" and r1["session"] == "regular"
        assert r1["source_time"] == "2026-10-01T15:00:00"
        assert r2["is_final"] is None and r2["source"] is None            # 誠實 NULL、不猜
        # 覆蓋語意＝最後寫入者：官方棒覆蓋同日 Yahoo 棒時血緣一併更新
        store.upsert_ohlcv_bars(_TK, "daily", [
            {"date": "2026-10-02", "open": 1, "high": 2, "low": 0.5, "close": 1.61, "volume": 11,
             "is_final": 1, "session": "regular", "source": "twse", "source_time": "2026-10-02T15:10:00"}])
        r2b = _select_all()[1]
        assert r2b["source"] == "twse" and r2b["is_final"] == 1 and r2b["close"] == 1.61
    finally:
        _cleanup()


def test_bar_is_final_rules():
    now = datetime(2026, 11, 12, 10, 0)                                  # 台北盤中
    assert df._bar_is_final("daily", "2026-11-12", now) == 0             # 當日盤中＝暫定
    assert df._bar_is_final("daily", "2026-11-11", now) == 1             # 昨日＝定案
    assert df._bar_is_final("daily", "2026-11-12", datetime(2026, 11, 12, 14, 30)) == 1   # 14:30 後定案
    assert df._bar_is_final("weekly", "2026-11-09", now) == 0            # 本週（週一起始）未到週五收盤
    assert df._bar_is_final("weekly", "2026-11-02", now) == 1            # 上週＝定案
    assert df._bar_is_final("hourly", "2026-11-12 09:00", now) == 1      # 已過整根小時
    assert df._bar_is_final("hourly", "2026-11-12 09:30", now) == 0      # 尚未滿 1 小時
    assert df._bar_is_final("daily", "bad-date", now) is None            # 判不出來＝NULL 不猜


def test_yf_rows_to_bars_carry_lineage():
    idx = pd.to_datetime(["2026-10-01", "2026-10-02"])
    h = pd.DataFrame({"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
                      "Close": [10.5, 11.5], "Volume": [1000.0, 2000.0]}, index=idx)
    bars = df._yf_rows_to_bars(h, "daily")
    assert all(b["source"] == "yfinance" and b["session"] == "regular" for b in bars)
    assert all(b["is_final"] == 1 for b in bars)                          # 歷史日＝定案
    assert all(b.get("source_time") for b in bars)


def test_official_bar_marked_final(monkeypatch):
    import stock_universe as su
    captured = {}
    monkeypatch.setattr(su, "get_official_bar",
                        lambda code: {"date": "2026-11-11", "open": 10, "high": 11, "low": 9,
                                      "close": 10.5, "volume": 1000, "source": "twse"})
    monkeypatch.setattr(store, "get_cached_ohlcv_bars", lambda t, tf, limit=1: [])
    monkeypatch.setattr(store, "upsert_ohlcv_bars",
                        lambda t, tf, bars: captured.setdefault("bar", bars[0]))
    df._apply_official_bar(_TK)
    b = captured["bar"]
    assert b["is_final"] == 1 and b["source"] == "twse" and b["session"] == "regular"
    assert b.get("source_time")


def test_finmind_fill_marked_final(monkeypatch):
    import finmind_data as finmind
    captured = {}
    monkeypatch.setattr(finmind, "newer_bars",
                        lambda code, last: [{"date": "2026-11-11", "open": 10, "high": 11,
                                             "low": 9, "close": 10.5, "volume": 1000}])
    monkeypatch.setattr(store, "get_ohlcv_last_date", lambda t, tf: "2026-11-10")
    monkeypatch.setattr(store, "upsert_ohlcv_bars",
                        lambda t, tf, bars: captured.setdefault("bar", bars[0]))
    n = df.fill_from_finmind(_TK)
    assert n == 1
    b = captured["bar"]
    assert b["is_final"] == 1 and b["source"] == "finmind" and b["session"] == "regular"
