"""美國財經日曆 econ_calendar（2026-10-27）。

使用者要首頁財經日曆涵蓋美國重要數據、顯示前值/預測值/公佈值，資料來源「兩者結合」
（FMP 主、FRED 備援）。用 mock 驗證：沒金鑰回 no_source（不編造）、FMP 解析正確
（只收美國、UTC→台北、中文對照、數值、重要度）、FRED 只補有對應的官方發布且不覆蓋 FMP。
"""
from datetime import timedelta

import pytest

import econ_calendar as ec


class _Resp:
    def __init__(self, data, code=200):
        self._d = data
        self.status_code = code

    def json(self):
        return self._d


@pytest.fixture(autouse=True)
def _clear():
    ec._cache.clear()
    yield
    ec._cache.clear()


def test_no_source_when_no_keys(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    out = ec.fetch_economic_calendar(force=True)
    assert out["status"] == "no_source" and out["events"] == []
    assert out["sources"] == {"fmp": False, "fred": False}


def test_fmp_parse_filter_tz_and_translate(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    rows = [
        {"date": "2026-10-27 12:15:00", "country": "US", "event": "ADP Employment Change",
         "previous": "1.85", "estimate": None, "actual": None, "impact": "High"},
        {"date": "2026-10-28 12:30:00", "country": "US", "event": "CPI (YoY)",
         "previous": "3.0", "estimate": "3.1", "actual": "3.2", "impact": "High"},
        {"date": "2026-10-28 09:00:00", "country": "EU", "event": "German Ifo",
         "previous": "1", "estimate": "1", "actual": None, "impact": "Medium"},   # 非美國 → 濾掉
    ]
    monkeypatch.setattr(ec.requests, "get", lambda url, **k: _Resp(rows))
    out = ec.fetch_economic_calendar(force=True)
    assert out["status"] == "ok" and out["sources"]["fmp"] is True
    evs = out["events"]
    assert all(e["event"] != "German Ifo" for e in evs)            # 非美國被濾掉
    adp = next(e for e in evs if "ADP" in e["zh"])
    assert adp["time"] == "20:15" and adp["date"] == "2026-10-27"  # UTC 12:15 → 台北 20:15
    assert adp["prev"] == 1.85 and adp["importance"] == "高"
    cpi = next(e for e in evs if "CPI" in e["zh"])
    assert cpi["estimate"] == 3.1 and cpi["actual"] == 3.2 and cpi["time"] == "20:30"


def test_fred_backstop_matches_and_dedup(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setenv("FRED_API_KEY", "f")
    fmp_rows = [{"date": "2026-10-28 12:30:00", "country": "US", "event": "Consumer Price Index",
                 "previous": "3.0", "estimate": "3.1", "actual": None, "impact": "High"}]
    fred = {"release_dates": [
        {"date": "2026-10-28", "release_name": "Consumer Price Index"},   # 與 FMP 同日同指標 → 不重複
        {"date": "2026-10-30", "release_name": "Employment Situation"},    # 有對應中文 → 補非農
        {"date": "2026-10-29", "release_name": "Some Obscure Release"},    # 無對應 → 略過
    ]}

    def fake_get(url, **k):
        return _Resp(fred) if "stlouisfed" in url else _Resp(fmp_rows)
    monkeypatch.setattr(ec.requests, "get", fake_get)
    out = ec.fetch_economic_calendar(force=True)
    assert out["sources"] == {"fmp": True, "fred": True}
    zhs = [e["zh"] for e in out["events"]]
    assert sum(1 for z in zhs if "CPI" in z) == 1                  # FMP 已有、FRED 不重複
    assert any("非農" in z for z in zhs)                            # FRED 補上 Employment Situation
    assert not any("Obscure" in e["event"] for e in out["events"])  # 無對應的不塞
    nfp = next(e for e in out["events"] if "非農" in e["zh"])
    assert nfp["source"] == "FRED" and nfp["estimate"] is None       # FRED 只有日期、無預測值


def test_fred_free_enrichment_prev_actual(monkeypatch):
    # FMP 付費端點不可用（無金鑰），改以 FRED series/observations 免費補前值/公佈值。
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "f")
    from datetime import datetime
    future = (datetime.now(ec._TPE) + timedelta(days=3)).strftime("%Y-%m-%d")
    today = datetime.now(ec._TPE).strftime("%Y-%m-%d")
    releases = {"release_dates": [
        {"date": future, "release_name": "Consumer Price Index"},       # 未來 → 前值=最後公佈、公佈待出
        {"date": today, "release_name": "Employment Situation"},         # 今日=已公佈 → 最新=公佈、次新=前值
    ]}
    cpi_obs = {"observations": [{"date": "2026-09-01", "value": "3.2"}, {"date": "2026-08-01", "value": "3.0"}]}
    nfp_obs = {"observations": [{"date": "2026-10-01", "value": "150"}, {"date": "2026-09-01", "value": "180"}]}

    def fake_get(url, **k):
        if "series/observations" in url:
            sid = (k.get("params") or {}).get("series_id")
            return _Resp(cpi_obs if sid == "CPIAUCSL" else nfp_obs)
        if "releases/dates" in url:
            return _Resp(releases)
        return _Resp({"Error": "n/a"}, 403)
    monkeypatch.setattr(ec.requests, "get", fake_get)
    out = ec.fetch_economic_calendar(force=True)
    evs = out["events"]
    cpi = next(e for e in evs if "CPI" in e["zh"])
    assert cpi["prev"] == 3.2 and cpi["actual"] is None          # 未來事件：前值=最後公佈、公佈值待出
    assert cpi["estimate"] is None and cpi["unit"] == "%（年增）"   # 預測值無免費來源；單位有標
    nfp = next(e for e in evs if "非農" in e["zh"])
    assert nfp["actual"] == 150 and nfp["prev"] == 180           # 今日已公佈：最新=公佈、次新=前值
    assert nfp["unit"] == "千人" and nfp["source"] == "FRED"


def test_graceful_on_bad_fmp(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(ec.requests, "get", lambda url, **k: _Resp({"Error Message": "limit"}))
    out = ec.fetch_economic_calendar(force=True)
    assert out["status"] == "no_source" and out["events"] == []     # 非 list → 當作沒來源，不崩潰
