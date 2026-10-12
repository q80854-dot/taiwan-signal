import time

import finmind_data as fm
import taifex as tf


def _fake(monkeypatch, table):
    """table: {dataset: rows 或 (rows, data_id 過濾不用)}；fm_get 直接回傳。"""
    def g(dataset, data_id=None, start=None, end=None, ttl=None, force=False):
        rows = table.get(dataset if not data_id else (dataset, data_id), table.get(dataset, []))
        return {"rows": rows, "ok": bool(rows), "stale": False, "fetched_at": time.time(), "error": None}
    monkeypatch.setattr(fm, "fm_get", g)


class _Resp:
    def __init__(self, j, code=200): self._j, self.status_code = j, code
    def json(self): return self._j


def test_fm_get_caches_and_falls_back_to_stale(monkeypatch):
    fm._cache.clear(); fm._calls.clear(); fm._blocked_until = 0
    n = {"c": 0}
    def get(url, params=None, headers=None, timeout=0):
        n["c"] += 1
        if n["c"] == 1:
            return _Resp({"status": 200, "data": [{"date": "2026-10-08", "close": 1}]})
        raise RuntimeError("boom")
    monkeypatch.setattr(fm.requests, "get", get)
    monkeypatch.setattr(fm.time, "sleep", lambda s: None)
    a = fm.fm_get("TaiwanStockPrice", "2330", "2026-10-01")
    assert a["ok"] and not a["stale"] and n["c"] == 1
    b = fm.fm_get("TaiwanStockPrice", "2330", "2026-10-01")           # 命中快取，不再請求
    assert b["rows"] == a["rows"] and n["c"] == 1
    c = fm.fm_get("TaiwanStockPrice", "2330", "2026-10-01", force=True)  # 請求失敗 → 沿用舊資料並標記 stale
    assert c["ok"] and c["stale"] and c["rows"] == a["rows"] and c["error"]


def test_fm_get_budget_guard(monkeypatch):
    fm._cache.clear(); fm._blocked_until = 0
    fm._calls.clear(); fm._calls.extend([time.time()] * fm.BUDGET)
    monkeypatch.setattr(fm.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該對外請求")))
    r = fm.fm_get("TaiwanStockPER", "2330", "2026-01-01")
    assert not r["ok"] and "額度" in r["error"]
    fm._calls.clear()


def test_fm_get_quota_response_pauses(monkeypatch):
    fm._cache.clear(); fm._calls.clear(); fm._blocked_until = 0
    monkeypatch.setattr(fm.requests, "get", lambda *a, **k: _Resp({"status": 402, "msg": "Requests reach the upper limit"}, 402))
    r = fm.fm_get("TaiwanStockNews", "2330", "2026-10-01")
    assert not r["ok"] and fm._blocked_until > time.time()
    fm._blocked_until = 0


def test_price_bars_skip_zero_volume_and_newer(monkeypatch):
    _fake(monkeypatch, {"TaiwanStockPrice": [
        {"date": "2026-10-07", "open": 10, "max": 11, "min": 9, "close": 10.5, "Trading_Volume": 5_000_000},
        {"date": "2026-10-08", "open": 10.5, "max": 12, "min": 10, "close": 11, "Trading_Volume": 8_000_000},
        {"date": "2026-10-12", "open": 0, "max": 0, "min": 0, "close": 0, "Trading_Volume": 0}]})
    bars, _ = fm.price_bars("2330")
    assert [b["date"] for b in bars] == ["2026-10-07", "2026-10-08"] and bars[-1]["volume"] == 8000
    assert [b["date"] for b in fm.newer_bars("2330", "2026-10-07")] == ["2026-10-08"]
    d = {"dates": ["2026-10-07"], "opens": [10], "highs": [11], "lows": [9], "closes": [10.5], "volumes": [5000]}
    assert fm.append_to_series(d, "2330") == 1 and d["dates"][-1] == "2026-10-08" and len(d["closes"]) == 2


def test_futures_inst_equivalent(monkeypatch):
    def row(who, lo, sh, d="2026-10-08"):
        return {"date": d, "institutional_investors": who, "long_open_interest_balance_volume": lo,
                "short_open_interest_balance_volume": sh}
    _fake(monkeypatch, {
        ("TaiwanFuturesInstitutionalInvestors", "TX"): [row("外資", 11000, 91000), row("投信", 100, 50), row("自營商", 4000, 4500)],
        ("TaiwanFuturesInstitutionalInvestors", "MTX"): [row("外資", 8000, 4000), row("投信", 0, 0), row("自營商", 0, 0)],
        ("TaiwanFuturesInstitutionalInvestors", "TMF"): [row("外資", 1000, 21000), row("投信", 0, 0), row("自營商", 0, 0)]})
    r = fm.futures_inst()
    f = r["items"]["外資及陸資"]
    assert r["date"] == "2026-10-08" and f["tx_net_oi"] == -80000
    assert f["equiv_net_oi"] == round(-80000 + 4000 * 0.25 - 20000 * 0.05)
    assert r["items"]["投信"]["equiv_net_oi"] == 50


def test_futures_inst_incomplete_day_is_ignored(monkeypatch):
    _fake(monkeypatch, {("TaiwanFuturesInstitutionalInvestors", "TX"): [
        {"date": "2026-10-12", "institutional_investors": "外資", "long_open_interest_balance_volume": 1, "short_open_interest_balance_volume": 1}]})
    assert fm.futures_inst()["ok"] is False


def test_futures_price_skips_placeholders_and_spreads(monkeypatch):
    base = {"futures_id": "TX", "open": 100, "max": 110, "min": 90, "spread": 5, "spread_per": 0.5, "settlement_price": 101}
    _fake(monkeypatch, {"TaiwanFuturesDaily": [
        {**base, "date": "2026-10-08", "contract_date": "202610", "close": 100, "volume": 90000, "open_interest": 70000, "trading_session": "position"},
        {**base, "date": "2026-10-08", "contract_date": "202611", "close": 99, "volume": 1000, "open_interest": 100, "trading_session": "position"},
        {**base, "date": "2026-10-08", "contract_date": "202612/202703", "close": 3, "volume": 10, "open_interest": 1, "trading_session": "position"},
        {**base, "date": "2026-10-08", "contract_date": "202610", "close": 101, "volume": 20000, "open_interest": 70000, "trading_session": "after_market"},
        {**base, "date": "2026-10-12", "contract_date": "202610", "close": 0, "volume": 0, "open_interest": 0, "trading_session": "after_market"}]})
    r = fm.futures_price()
    assert r["date"] == "2026-10-08" and r["latest"]["contract"] == "202610" and r["latest"]["close"] == 100
    assert r["night"]["close"] == 101 and len(r["days"]) == 1


def test_chips_and_streak(monkeypatch):
    def r(d, n, b, s): return {"date": d, "name": n, "buy": b, "sell": s}
    rows = []
    for i, d in enumerate(["2026-10-06", "2026-10-07", "2026-10-08"]):
        rows += [r(d, "Foreign_Investor", 3_000_000, 1_000_000), r(d, "Investment_Trust", 100_000, 0), r(d, "Dealer_self", 0, 500_000)]
    _fake(monkeypatch, {"TaiwanStockInstitutionalInvestorsBuySell": rows})
    c = fm.sec_chips("2330")
    assert c["days"][-1]["foreign"] == 2000 and c["days"][-1]["dealer"] == -500
    assert c["sum5"]["trust"] == 300 and c["streak"]["foreign"] == 3 and c["date"] == "2026-10-08"


def test_revenue_yoy_mom(monkeypatch):
    rows = [{"revenue_year": 2025, "revenue_month": 9, "revenue": 100}, {"revenue_year": 2025, "revenue_month": 8, "revenue": 90},
            {"revenue_year": 2026, "revenue_month": 8, "revenue": 99}, {"revenue_year": 2026, "revenue_month": 9, "revenue": 120}]
    _fake(monkeypatch, {"TaiwanStockMonthRevenue": rows})
    r = fm.sec_revenue("2330")
    lt = r["latest"]
    assert lt["ym"] == "2026-09" and lt["yoy"] == 20.0 and lt["mom"] == round((120 / 99 - 1) * 100, 1)
    assert r["ytd_yoy"] == round(((99 + 120) / (90 + 100) - 1) * 100, 1)


def test_valuation_percentile(monkeypatch):
    rows = [{"date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "PER": 10 + i * 0.1, "PBR": 2, "dividend_yield": 1.5} for i in range(120)]
    _fake(monkeypatch, {"TaiwanStockPER": rows})
    v = fm.sec_valuation("2330")
    assert v["per"]["pctile"] == 100 and v["yield"] == 1.5


def test_stock_pack_rejects_bad_code_and_survives_empty(monkeypatch):
    assert "error" in fm.stock_pack("../etc")
    _fake(monkeypatch, {})
    p = fm.stock_pack("2330")
    assert p["code"] == "2330" and len(p["failed"]) == len(fm.SECTIONS) and p["insights"] == []


def test_news_links_sanitized(monkeypatch):
    _fake(monkeypatch, {"TaiwanStockNews": [
        {"date": "2026-10-08 10:00:00", "title": "好消息", "source": "x", "link": "javascript:alert(1)"},
        {"date": "2026-10-08 09:00:00", "title": "另一則", "source": "y", "link": "https://example.com/a"}]})
    items = fm.sec_news("2330")["items"]
    assert items[0]["link"] == "" and items[1]["link"].startswith("https://")


def test_taifex_prefers_newer_finmind(monkeypatch):
    monkeypatch.setattr(fm, "futures_inst", lambda days=12: {"ok": True, "date": "2026-10-08", "items": {"外資及陸資": {"equiv_net_oi": -5}}})
    v = tf.apply_finmind({"futures": {"date": "2026-10-07", "items": {"外資及陸資": {"equiv_net_oi": -1}}}, "stale": ["futures"]})
    assert v["futures"]["date"] == "2026-10-08" and v["futures"]["source"] == "finmind" and v["stale"] == []
    monkeypatch.setattr(fm, "futures_inst", lambda days=12: {"ok": True, "date": "2026-10-06", "items": {"x": {}}})
    v2 = tf.apply_finmind({"futures": {"date": "2026-10-07", "items": {"a": 1}}})
    assert v2["futures"]["date"] == "2026-10-07"           # FinMind 較舊就不覆蓋
    monkeypatch.setattr(fm, "futures_inst", lambda days=12: {"ok": False, "date": None, "items": {}})
    assert tf.apply_finmind({"futures": {"date": "2026-10-07"}})["futures"]["date"] == "2026-10-07"


def test_cashflow_cumulative_to_single_quarter(monkeypatch):
    def r(d, v): return {"date": d, "type": "CashFlowsFromOperatingActivities", "value": v}
    _fake(monkeypatch, {"TaiwanStockCashFlowsStatement": [r("2025-12-31", 2275), r("2026-03-31", 699), r("2026-06-30", 1500), r("2025-09-30", 1549)]})
    c = fm.sec_cashflow("2330")
    got = {x["date"]: x["ocf"] for x in c["quarters"]}
    assert got == {"2025-12-31": 726, "2026-03-31": 699, "2026-06-30": 801}   # 2025Q3 缺 Q2 無法還原單季，不顯示


def test_token_rejected_falls_back_to_anonymous(monkeypatch):
    fm._cache.clear(); fm._calls.clear(); fm._blocked_until = 0; fm._token_bad_until = 0
    monkeypatch.setattr(fm, "TOKEN", "fake-token")
    seen = []

    def get(url, params=None, headers=None, timeout=0):
        seen.append(bool(headers))
        if headers:      # 帶 token 被拒
            return _Resp({"status": 402, "msg": "Requests reach the upper limit"}, 402)
        return _Resp({"status": 200, "data": [{"date": "2026-10-08"}]})
    monkeypatch.setattr(fm.requests, "get", get)
    monkeypatch.setattr(fm.time, "sleep", lambda s: None)
    r = fm.fm_get("TaiwanStockPER", "2330", "2026-10-01")
    assert r["ok"] and seen == [True, False] and fm._blocked_until == 0
    fm.fm_get("TaiwanStockPER", "2330", "2026-10-02")      # token 暫停期間直接用免登入
    assert seen[-1] is False and fm._token_bad_until > time.time()
    fm._token_bad_until = 0


def test_indicators_from_raw_statements(monkeypatch):
    def rows(d, **kw): return [{"date": d, "type": k, "value": v} for k, v in kw.items()]
    inc = (rows("2025-06-30", Revenue=1000, GrossProfit=400, OperatingIncome=200, IncomeAfterTaxes=160, EquityAttributableToOwnersOfParent=150, EPS=1.0)
           + rows("2026-06-30", Revenue=1200, GrossProfit=540, OperatingIncome=300, IncomeAfterTaxes=240, EquityAttributableToOwnersOfParent=225, EPS=1.5))
    bs = (rows("2025-06-30", TotalAssets=5000, Equity=2000, EquityAttributableToOwnersOfParent=1500, CurrentAssets=1500, CurrentLiabilities=1000)
          + rows("2026-06-30", TotalAssets=6000, Equity=2400, EquityAttributableToOwnersOfParent=1800, CurrentAssets=2400, CurrentLiabilities=1200))
    cf = (rows("2026-03-31", CashFlowsFromOperatingActivities=300, PropertyAndPlantAndEquipment=-100)       # FinMind 實際科目名
          + rows("2026-06-30", CashFlowsFromOperatingActivities=700, PropertyAndPlantAndEquipment=-250))
    _fake(monkeypatch, {"TaiwanStockFinancialStatements": inc, "TaiwanStockBalanceSheet": bs, "TaiwanStockCashFlowsStatement": cf})
    r = fm.sec_indicators("2330")
    m = {x["key"]: {y["period"]: y for y in x["rows"]} for x in r["metrics"]}
    q = "2026/Q2"
    assert m["eps"][q]["value"] == 1.5 and m["eps"][q]["yoy"] == 50.0
    assert m["gross"][q]["value"] == 45.0 and m["gross"][q]["yoy_pp"] == 5.0      # 45% vs 40%
    assert m["roe"][q]["value"] == 12.5                                            # 225/1800，歸屬母公司
    assert m["roa"][q]["value"] == 4.0                                             # 240/6000
    assert m["current_ratio"][q]["value"] == 200.0 and m["equity_ratio"][q]["value"] == 40.0
    assert q in m["fcf"] and m["fcf"][q]["value"] == round(((700 - 300) - (250 - 100)) / 1e8, 2)    # 單季＝累計相減
    assert r["ok"] and r["date"] == "2026-06-30"
    assert fm.stock_pack("2330")["indicators"]["ok"]
