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


def _route(release_dates=None, obs=None):
    """建立 fake requests.get：依 URL 路由 FRED 三端點（series/observations、series/release、
    releases/dates）。series/release 回傳每個數列所屬 release_id（權威對應）。"""
    sid2rid = {"PAYEMS": 50, "CPIAUCSL": 10, "ICSA": 180, "PCEPI": 54, "RSAFS": 25}

    def fake_get(url, **k):
        p = k.get("params") or {}
        if "series/observations" in url:
            return _Resp(obs.get(p.get("series_id"), {"observations": []}) if obs else {"observations": []})
        if "series/release" in url:
            rid = sid2rid.get(p.get("series_id"))
            return _Resp({"releases": [{"id": rid}]}) if rid else _Resp({"releases": []})
        if "releases/dates" in url:
            return _Resp({"release_dates": release_dates or []})
        return _Resp({"Error": "n/a"}, 403)
    return fake_get


def test_fred_release_whitelist_only(monkeypatch):
    # 只列白名單 release_id（含 series/release 動態補出的）；每日資料如 Federal Funds Data 不列。
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "f")
    from datetime import datetime
    d = (datetime.now(ec._TPE) + timedelta(days=2)).strftime("%Y-%m-%d")
    releases = [
        {"date": d, "release_id": 10, "release_name": "Consumer Price Index"},                 # 靜態白名單 → 列
        {"date": d, "release_id": 180, "release_name": "Unemployment Insurance Weekly Claims"}, # series/release 動態補 → 列
        {"date": d, "release_id": 9999, "release_name": "Federal Funds Data"},                  # 每日資料、非白名單 → 不列
    ]
    monkeypatch.setattr(ec.requests, "get", _route(release_dates=releases))
    out = ec.fetch_economic_calendar(force=True)
    zhs = [e["zh"] for e in out["events"]]
    assert any("CPI" in z for z in zhs)                                   # 白名單 10 → 列
    assert any("初領失業" in z for z in zhs)                               # 動態補 180 → 列
    assert not any("Federal Funds" in e["event"] for e in out["events"])  # 每日資料不列（根因修正）
    assert not any("Obscure" in str(e) for e in out["events"])


def test_fred_free_enrichment_prev_actual(monkeypatch):
    # FMP 付費端點不可用（無金鑰），改以 FRED series/observations 免費補前值/公佈值。
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "f")
    from datetime import datetime
    future = (datetime.now(ec._TPE) + timedelta(days=3)).strftime("%Y-%m-%d")
    today = datetime.now(ec._TPE).strftime("%Y-%m-%d")
    releases = [
        {"date": future, "release_id": 10, "release_name": "Consumer Price Index"},   # 未來 → 前值=最後公佈、公佈待出
        {"date": today, "release_id": 50, "release_name": "Employment Situation"},     # 今日=已公佈 → 最新=公佈、次新=前值
    ]
    obs = {
        "CPIAUCSL": {"observations": [{"date": "2026-09-01", "value": "3.2"}, {"date": "2026-08-01", "value": "3.0"}]},
        "PAYEMS": {"observations": [{"date": "2026-10-01", "value": "150"}, {"date": "2026-09-01", "value": "180"}]},
    }
    monkeypatch.setattr(ec.requests, "get", _route(release_dates=releases, obs=obs))
    out = ec.fetch_economic_calendar(force=True)
    evs = out["events"]
    cpi = next(e for e in evs if "CPI" in e["zh"])
    assert cpi["prev"] == 3.2 and cpi["actual"] is None          # 未來事件：前值=最後公佈、公佈值待出
    assert cpi["estimate"] is None and cpi["unit"] == "%（年增）"   # 預測值無免費來源；單位有標
    nfp = next(e for e in evs if "非農" in e["zh"])
    assert nfp["actual"] == 150 and nfp["prev"] == 180           # 今日已公佈：最新=公佈、次新=前值
    assert nfp["unit"] == "千人" and nfp["source"] == "FRED"


def test_fomc_only_from_official_schedule(monkeypatch):
    # FOMC 來自 Fed 官方寫死日程，只在決議日出現、不天天寫、無編造數值。
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "f")
    from datetime import datetime
    d = (datetime.now(ec._TPE) + timedelta(days=1)).strftime("%Y-%m-%d")
    # 給一筆白名單發布讓 sources.fred=True（FOMC 僅在有實際來源時加入）
    releases = [{"date": d, "release_id": 10, "release_name": "Consumer Price Index"}]
    monkeypatch.setattr(ec.requests, "get", _route(release_dates=releases))
    out = ec.fetch_economic_calendar(days_ahead=420, force=True)
    frm, to = out["window"]["from"], out["window"]["to"]
    exp = sorted({datetime.fromisoformat(i).astimezone(ec._TPE).strftime("%Y-%m-%d")
                  for i in ec._FOMC_DECISIONS_UTC
                  if frm <= datetime.fromisoformat(i).astimezone(ec._TPE).strftime("%Y-%m-%d") <= to})
    fomc = [e for e in out["events"] if "FOMC" in e["zh"]]
    assert sorted(e["date"] for e in fomc) == exp                    # 只在官方決議日（換算台北）
    assert len(fomc) == len(set(e["date"] for e in fomc))            # 不重複、不天天寫
    assert all(e["source"] == "Fed" and e["prev"] is None and e["estimate"] is None
               and e["actual"] is None for e in fomc)                # 無編造數值


def test_graceful_on_bad_fmp(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(ec.requests, "get", lambda url, **k: _Resp({"Error Message": "limit"}))
    out = ec.fetch_economic_calendar(force=True)
    assert out["status"] == "no_source" and out["events"] == []     # 非 list → 當作沒來源，不崩潰
