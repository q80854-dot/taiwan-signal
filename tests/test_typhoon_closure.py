"""颱風／天災臨時休市：全自動偵測（2026-10-23）。

使用者反映「今天放颱風假」，要系統「自行應對、知道放假所以休市，但掃描抓資料能力要繼續存在」，
並選擇「全自動偵測」。颱風假不在預先公布的官方假日清單裡，這裡以官方即時行情（MIS 2330 的
成交日期）反推今天到底有沒有實際開盤：
  - 正常交易日：MIS 2330 日期＝今天 → 不是休市。
  - 颱風休市：市場沒開，MIS 停在前一交易日 → 判定今日臨時休市。
  - 抓不到（網路問題）：無法判定 → 視為有開盤（保守，不把正常交易日誤判成休市）。
一旦判定休市就整天沿用；11:00 前不判（避免早盤誤判）。
"""
from datetime import datetime, timedelta

import pytest

import data_fetcher as df


def _a_weekday(h=11, m=30):
    d = datetime(2026, 10, 23, h, m)
    while d.weekday() >= 5:          # 保證落在平日，與實際日曆無關
        d += timedelta(days=1)
    return d


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # 國定假日一律判否（回空集合，且不打網路）；颱風偵測獨立驗證
    monkeypatch.setattr(df, "_fetch_official_holiday_set", lambda: set())
    # meta 存取改記憶體，避免 set_meta("typhoon_closure") 在測試間互相汙染
    from state_store import store
    mem = {}
    monkeypatch.setattr(store, "get_meta", lambda k, d=None: mem.get(k, d))
    monkeypatch.setattr(store, "set_meta", lambda k, v: mem.__setitem__(k, v))
    df._reset_typhoon_state()
    yield
    df._reset_typhoon_state()


def _set_mis(monkeypatch, date_str):
    """讓 realtime.fetch_mis 回傳 2330 一筆、成交日期為 date_str（None＝抓不到）。"""
    import realtime
    if date_str is None:
        monkeypatch.setattr(realtime, "fetch_mis", lambda *a, **k: {})
    else:
        monkeypatch.setattr(realtime, "fetch_mis",
                            lambda *a, **k: {"2330": {"code": "2330", "date": date_str, "price": 1000.0}})


def test_probe_today_true_false_unknown(monkeypatch):
    now = _a_weekday()
    today = now.strftime("%Y-%m-%d")
    _set_mis(monkeypatch, today);        assert df._market_probe_today(now) is True
    _set_mis(monkeypatch, "2000-01-01"); assert df._market_probe_today(now) is False
    _set_mis(monkeypatch, None);         assert df._market_probe_today(now) is None


def test_typhoon_detected_when_market_did_not_trade(monkeypatch):
    now = _a_weekday(11, 30)
    _set_mis(monkeypatch, "2000-01-01")                  # 停在前一交易日＝今天沒開盤
    assert df.refresh_typhoon_state(now) == "typhoon"
    assert df._closure_reason(now) == "typhoon"
    assert df._is_tw_market_holiday(now) is True


def test_normal_trading_day_not_closed(monkeypatch):
    now = _a_weekday(11, 30)
    _set_mis(monkeypatch, now.strftime("%Y-%m-%d"))      # 今天有成交
    assert df.refresh_typhoon_state(now) is None
    assert df._is_tw_market_holiday(now) is False


def test_no_false_close_before_cutoff(monkeypatch):
    now = _a_weekday(10, 0)                               # 11:00 前
    _set_mis(monkeypatch, "2000-01-01")                  # 即使資料看起來停在舊日期
    assert df.refresh_typhoon_state(now) is None          # 也不在早盤判休市
    assert df._is_tw_market_holiday(now) is False


def test_outage_stays_open(monkeypatch):
    now = _a_weekday(11, 30)
    _set_mis(monkeypatch, None)                          # 抓不到
    assert df.refresh_typhoon_state(now) is None          # 無法判定＝視為有開盤
    assert df._is_tw_market_holiday(now) is False


def test_sticky_once_resolved(monkeypatch):
    now = _a_weekday(11, 30)
    _set_mis(monkeypatch, "2000-01-01")
    assert df.refresh_typhoon_state(now) == "typhoon"
    _set_mis(monkeypatch, now.strftime("%Y-%m-%d"))      # 之後就算行情恢復
    assert df.refresh_typhoon_state(now) == "typhoon"     # 當日仍沿用休市判定（節流＋resolved）


def test_get_market_session_reports_typhoon(monkeypatch):
    fixed = _a_weekday(12, 0)

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr(df, "datetime", _FakeDT)
    monkeypatch.setattr(df, "_closure_reason", lambda now: "typhoon")
    s = df.get_market_session()
    assert s["session"] == "holiday" and s["reason"] == "typhoon" and "颱風" in s["session_zh"]

    monkeypatch.setattr(df, "_closure_reason", lambda now: "national")
    s2 = df.get_market_session()
    assert s2["session"] == "holiday" and s2["reason"] == "national" and s2["session_zh"] == "國定假日休市"
