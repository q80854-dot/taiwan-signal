"""官方休市清單抓取的失敗負快取（2026-11-04）。

TWSE holidaySchedule 偶爾回非 JSON（空/HTML）。修正前每次呼叫都重打網路＋噴一次
WARNING，掃描/盤中大量呼叫時每秒洗版。修正後：失敗在冷卻期內直接退回靜態清單，
不重打、不洗 log（假日判斷正確性由 config 靜態清單保證）。
"""
import data_fetcher as df


def test_holiday_fetch_negative_cache(monkeypatch):
    monkeypatch.setattr(df, "_holiday_fail_until", 0.0, raising=False)
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)   # 強制 miss，走抓取路徑
    calls = {"n": 0}

    class _Bad:
        status_code = 200
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def _get(*a, **k):
        calls["n"] += 1
        return _Bad()

    monkeypatch.setattr(df.requests, "get", _get)
    assert df._fetch_official_holiday_set() is None
    assert df._fetch_official_holiday_set() is None
    assert calls["n"] == 1, f"負快取失效：重打了 {calls['n']} 次"


def test_holiday_fetch_http_error_negative_cache(monkeypatch):
    monkeypatch.setattr(df, "_holiday_fail_until", 0.0, raising=False)
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)
    calls = {"n": 0}

    class _Err:
        status_code = 503
        def json(self):
            return []

    def _get(*a, **k):
        calls["n"] += 1
        return _Err()

    monkeypatch.setattr(df.requests, "get", _get)
    assert df._fetch_official_holiday_set() is None
    assert df._fetch_official_holiday_set() is None
    assert calls["n"] == 1
