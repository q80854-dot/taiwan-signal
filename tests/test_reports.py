"""每日盤前／盤後報告（2026-10-16）。驗證：
  - 事實蒐集只用已驗證數字、抓不到標「暫無」且列入 missing（不編造）。
  - 無 ANTHROPIC_API_KEY 時走規則式摘要、報告照常產生並存取。
  - 規則式摘要只引用提供的數字。
  - fetch_global_markets 的 us10y（^TNX x10）換算成百分比。
"""
import os
import json
import importlib
import pytest


class _FakeStore:
    def __init__(self):
        self.m = {}
    def set_meta(self, k, v):
        self.m[k] = v
    def get_meta(self, k, default=None):
        return self.m.get(k, default)


def _fake_global(**over):
    g = {
        "twii":   {"price": 23412.0, "chg": 0.8, "source": "twse"},
        "tpex":   {"price": 268.4, "chg": 0.6, "source": "tpex"},
        "sp500":  {"price": 5800.0, "chg": 0.5, "source": "yfinance"},
        "nasdaq": {"price": 20500.0, "chg": 0.9, "source": "yfinance"},
        "dow":    {"price": 43000.0, "chg": 0.3, "source": "yfinance"},
        "sox":    {"price": 5200.0, "chg": 1.2, "source": "yfinance"},
        "vix":    {"price": 15.3, "chg": -2.0, "source": "yfinance"},
        "dxy":    {"price": 103.2, "chg": 0.1, "source": "yfinance"},
        "us10y":  {"price": 4.23, "chg": 0.5, "source": "yfinance"},
        "wti":    {"price": 72.1, "chg": -1.1, "source": "yfinance"},
        "brent":  {"price": 76.5, "chg": -0.9, "source": "yfinance"},
        "gold":   {"price": 2650.0, "chg": 0.4, "source": "yfinance_GC=F"},
        "btc":    {"price": 68000.0, "chg": 2.0, "source": "coingecko"},
        "usdtwd": {"price": 31.84, "chg": -0.05, "source": "yfinance"},
    }
    g.update(over)
    return g


@pytest.fixture
def reports(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    import reports as rp
    importlib.reload(rp)
    import data_fetcher as df
    monkeypatch.setattr(df, "fetch_global_markets", lambda force=False: _fake_global())
    monkeypatch.setattr(df, "fetch_market_overview", lambda: {
        "foreign": {"net_buy_twd": 18_200_000_000}, "sentiment_score": 62, "sentiment_zh": "偏多"})
    import fundamentals as fd
    monkeypatch.setattr(fd, "fetch_all_news_rows", lambda: [
        {"code": "2330", "name": "台積電", "subject": "公告法說會訊息", "date": "20261016", "time": "100000", "negative": False},
        {"code": "6666", "name": "某公司", "subject": "本公司受重大訴訟", "date": "20261016", "time": "090000", "negative": True},
    ])
    return rp


def test_collect_facts_structure(reports):
    f = reports._collect_facts("postmarket")
    titles = [s["title"] for s in f["sections"]]
    assert titles == ["美股四大指數", "原油與商品", "利率與匯率", "台股盤後"]
    # 台股盤後含外資與情緒
    tw = f["sections"][-1]["items"]
    labels = [it["label"] for it in tw]
    assert "加權指數" in labels and "外資買賣超" in labels and "市場情緒分數" in labels
    assert not f["missing"]  # 全部有值


def test_missing_marked_not_fabricated(reports, monkeypatch):
    import data_fetcher as df
    g = _fake_global(wti={"price": None, "source": "error"})
    monkeypatch.setattr(df, "fetch_global_markets", lambda force=False: g)
    f = reports._collect_facts("premarket")
    assert "WTI 原油" in f["missing"]
    wti = reports._find(f, "WTI 原油")
    assert wti["value"] == "暫無" and wti["raw"] is None


def test_rule_narrative_only_uses_given(reports):
    f = reports._collect_facts("postmarket")
    text = reports._rule_narrative("postmarket", f)
    assert "標普 500" in text and "加權指數" in text
    assert "23,412" in text  # 用了提供的數字


def test_ai_narrative_none_without_key(reports):
    f = reports._collect_facts("postmarket")
    assert reports._ai_narrative("postmarket", f) is None


def test_build_and_get_report_rule_mode(reports):
    store = _FakeStore()
    rep = reports.build_report("postmarket", store=store)
    assert rep["narrative_source"] == "rule"
    assert rep["kind"] == "postmarket" and rep["narrative"]
    got = reports.get_report("postmarket", store=store)
    assert got and got["date"] == rep["date"]
    # 存了以日期為 key 的版本（供 boot catch-up 判斷今天是否已產生）
    assert any(k.startswith("report_postmarket_2") for k in store.m)


def test_llm_provider_resolution(monkeypatch):
    import llm, importlib
    importlib.reload(llm)
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    assert llm.provider_name() is None and llm.available() is False
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert llm.provider_name() == "groq"
    monkeypatch.setenv("GEMINI_API_KEY", "y")   # 免費的優先序：gemini 在 groq 之前
    assert llm.provider_name() == "gemini"
    monkeypatch.setenv("LLM_PROVIDER", "groq")  # 可強制指定
    assert llm.provider_name() == "groq"


def test_llm_usage_tracking(monkeypatch):
    import llm, importlib, state_store
    importlib.reload(llm)

    class _FS:
        def __init__(self): self.m = {}
        def get_meta(self, k, d=None): return self.m.get(k, d)
        def set_meta(self, k, v): self.m[k] = v
    monkeypatch.setattr(state_store, "store", _FS())
    llm._record_usage("gemini", "gemini-x", 100, 40)
    llm._record_usage("gemini", "gemini-x", 50, 10)
    llm._record_usage("groq", "llama", 20, 5)
    u = llm.usage()
    assert u["total"]["gemini"] == {"calls": 2, "in": 150, "out": 50}
    assert u["total"]["groq"] == {"calls": 1, "in": 20, "out": 5}
    assert u["last"]["provider"] == "groq" and u["last"]["out"] == 5
    # 今日逐日桶也有累積
    day = next(iter(u["by_day"]))
    assert u["by_day"][day]["gemini"]["calls"] == 2


def test_gemini_retries_on_retired_model(monkeypatch):
    import llm, importlib
    importlib.reload(llm)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")  # 已淘汰
    calls = {"n": 0}

    class R:
        def __init__(self, code, payload=None, text=""):
            self.status_code = code; self._p = payload or {}; self.text = text
        def json(self): return self._p

    def fake_post(url, params=None, json=None, timeout=None):
        calls["n"] += 1
        if "gemini-2.0-flash" in url:
            return R(404, text='{"error":{"message":"This model models/gemini-2.0-flash is no longer available. Please update your code to use models/gemini-3.8-flash"}}')
        return R(200, {"candidates": [{"content": {"parts": [{"text": "【測試】新模型回傳。"}]}}]})
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = llm.complete("sys", "user", 100)
    assert out == "【測試】新模型回傳。" and calls["n"] == 2  # 第一次 404、第二次用建議模型成功


def test_gemini_retries_on_transient_503(monkeypatch):
    import llm, importlib, time
    importlib.reload(llm)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # 不要真的等
    calls = {"n": 0}

    class R:
        def __init__(self, code, payload=None, text=""):
            self.status_code = code; self._p = payload or {}; self.text = text
        def json(self): return self._p

    def fake_post(url, params=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return R(503, text='{"error":{"code":503,"message":"The model is overloaded. Please try again later."}}')
        return R(200, {"candidates": [{"content": {"parts": [{"text": "【測試】重試後成功。"}]}}]})
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = llm.complete("sys", "user", 100)
    assert out == "【測試】重試後成功。" and calls["n"] == 2


def test_gemini_retries_on_read_timeout(monkeypatch):
    import llm, importlib, time, requests
    importlib.reload(llm)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class R:
        status_code = 200
        text = ""
        def json(self): return {"candidates": [{"content": {"parts": [{"text": "【測試】逾時後重試成功。"}]}}]}

    def fake_post(url, params=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.Timeout("read timed out")
        return R()
    monkeypatch.setattr(requests, "post", fake_post)
    out = llm.complete("sys", "user", 100)
    assert out == "【測試】逾時後重試成功。" and calls["n"] == 2


def test_rule_narrative_has_summary_and_watch(reports):
    f = reports._collect_facts("postmarket")
    text = reports._rule_narrative("postmarket", f)
    assert "今日重點摘要" in text and "後市觀察與風險" in text and "消息面" in text


def test_breadth_from_snapshot(reports, monkeypatch):
    import state_store
    monkeypatch.setattr(state_store.store, "get_meta",
                        lambda k, default=None: ({"breadth": {"advancers": 612, "decliners": 318, "limit_up": 7, "limit_down": 2}}
                                                 if k == "intraday_snapshot" else default))
    f = reports._collect_facts("postmarket")
    tw = [it["label"] for it in f["sections"][-1]["items"]]
    assert "上漲／下跌家數" in tw and "漲停／跌停家數" in tw
    txt = reports._facts_as_text(f)
    assert "612 / 318" in txt


def test_news_collected_into_facts(reports):
    f = reports._collect_facts("premarket")
    assert f.get("news") and len(f["news"]) == 2
    assert f["news"][0]["negative"] is True   # 負面優先露出
    txt = reports._facts_as_text(f)
    assert "重大訊息" in txt and "重大訴訟" in txt


def test_build_stock_report_rule_mode(reports, monkeypatch):
    import fundamentals as fd
    monkeypatch.setattr(fd, "fetch_valuation_map", lambda: {"2330": {"pe": 20.1, "pb": 5.2, "yield_pct": 1.8}})
    monkeypatch.setattr(fd, "fetch_monthly_revenue_map", lambda: {"2330": {"period": "202609", "yoy_pct": 12.3, "mom_pct": 2.1, "cum_yoy_pct": 15.0}})
    monkeypatch.setattr(fd, "get_profitability_quality", lambda c: {"period": "2026Q2", "gross_margin_pct": 54.0, "operating_margin_pct": 42.0})
    import sys, types
    stub = types.ModuleType("stock_universe")
    stub.get_stock_info_any = lambda t: {"name": "台積電", "sector": "半導體", "market": "上市", "close": 1085, "change_pct": 1.4} if t.startswith("2330") else None
    monkeypatch.setitem(sys.modules, "stock_universe", stub)
    import finmind_data as fm
    monkeypatch.setattr(fm, "sec_dividend", lambda c: {"ok": True, "summary": {
        "latest_year": "2025", "latest_cash": 16.0, "latest_stock": 0.0,
        "consecutive_cash": 7, "avg_cash_recent": 13.5, "avg_years": 5, "years_window": 8}})
    store = _FakeStore()
    rep = reports.build_stock_report("2330", store=store)
    assert rep["narrative_source"] == "rule" and rep["code"] == "2330"
    assert "月營收" in rep["fields"] and "估值面" in rep["fields"]
    assert "股利政策" in rep["fields"] and rep["fields"]["股利政策"]["連續配息年數"] == 7
    assert "股利政策" in rep["narrative"] and "連續配息 7 年" in rep["narrative"]
    assert "12.3" in rep["narrative"] or "12" in rep["narrative"]
    got = reports.get_stock_report("2330", store=store)
    assert got and got["code"] == "2330"


def test_global_markets_us10y_as_percent(monkeypatch):
    import data_fetcher as df
    importlib.reload(df)
    monkeypatch.setattr(df, "fetch_market_index", lambda: {})
    def fake_single(sym, rng=None):
        if sym == "^TNX":
            # yfinance 的 ^TNX 直接回傳百分比（例如 5.10 代表 5.10%），不再除以 10
            return {"price": 5.10, "prev": 5.08, "chg": 0.4, "chg_pt": 0.02, "source": "yfinance"}
        return None
    monkeypatch.setattr(df, "_fetch_yf_single", fake_single)
    df._cache_clear("global_markets") if hasattr(df, "_cache_clear") else None
    g = df.fetch_global_markets(force=True)
    assert abs(g["us10y"]["price"] - 5.10) < 0.001  # 不換算，直接顯示 5.10%
