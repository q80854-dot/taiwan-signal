"""2026-10-16 根因修復：STOCK_DAY_ALL 從 Render 常逾時且 DB 存到『只有上櫃』的半套清單
後一直沿用，導致上市整個消失。驗證：
1. STOCK_DAY_ALL／www 報表都失效時，以 BWIBBU_ALL 代號＋MIS 官方報價建上市清單。
2. 快取缺一整個市場會被判定為壞掉、需重抓（但 10 分鐘內節流）。"""
import time

import stock_universe as su


def test_twse_list_from_mis_builds_from_valuation_and_mis(monkeypatch):
    monkeypatch.setattr(su, "_twse_codes_from_valuation", lambda: {"2317": "鴻海", "2330": "台積電", "9999": "停牌股"})
    quotes = {
        "2317": {"price": 201.0, "prev_close": 198.0, "open": 199.0, "high": 202.0, "low": 198.5,
                 "volume_lots": 12000, "date": "2026-10-16"},
        "2330": {"price": 1010.0, "prev_close": 1000.0, "open": 1005.0, "high": 1015.0, "low": 1000.0,
                 "volume_lots": 25000, "date": "2026-10-16"},
        # 9999 無報價 → 略過（不臆測）
    }
    import realtime
    monkeypatch.setattr(realtime, "fetch_mis", lambda pairs, **k: quotes)
    rows = su._twse_list_from_mis()
    by = {r["code"]: r for r in rows}
    assert set(by) == {"2317", "2330"}, "沒有報價的代號應被略過"
    assert by["2317"]["market"] == "TSE" and by["2317"]["ticker"] == "2317.TW"
    assert by["2317"]["close"] == 201.0 and by["2317"]["name"] == "鴻海"
    assert by["2317"]["change"] == 3.0 and by["2317"]["change_pct"] > 0
    assert by["2330"]["volume_lots"] == 25000 and by["2330"]["quote_date"] == "2026-10-16"


def test_fetch_twse_list_falls_through_to_mis(monkeypatch):
    monkeypatch.setattr(su, "_twse_list_openapi", lambda: [])
    monkeypatch.setattr(su, "_twse_list_from_report", lambda: [])
    monkeypatch.setattr(su, "_twse_list_from_mis", lambda: [{"code": "2317", "market": "TSE"}])
    assert su._fetch_twse_list() == [{"code": "2317", "market": "TSE"}]


def test_market_coverage_bad():
    both = {"stocks": [{"market": "TSE"}, {"market": "OTC"}], "fetched_at": time.time()}
    assert su._market_coverage_bad(both) is False
    empty = {"stocks": [], "fetched_at": time.time()}
    assert su._market_coverage_bad(empty) is True
    # 只有上櫃：剛抓到（<10 分鐘）先不重試（節流），超過 10 分鐘才判定需重抓
    otc_fresh = {"stocks": [{"market": "OTC"}], "fetched_at": time.time()}
    assert su._market_coverage_bad(otc_fresh) is False
    otc_old = {"stocks": [{"market": "OTC"}], "fetched_at": time.time() - 700}
    assert su._market_coverage_bad(otc_old) is True
