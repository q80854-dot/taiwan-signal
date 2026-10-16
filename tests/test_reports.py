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
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import reports as rp
    importlib.reload(rp)
    import data_fetcher as df
    monkeypatch.setattr(df, "fetch_global_markets", lambda force=False: _fake_global())
    monkeypatch.setattr(df, "fetch_market_overview", lambda: {
        "foreign": {"net_buy_twd": 18_200_000_000}, "sentiment_score": 62, "sentiment_zh": "偏多"})
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


def test_global_markets_us10y_scaled(monkeypatch):
    import data_fetcher as df
    importlib.reload(df)
    monkeypatch.setattr(df, "fetch_market_index", lambda: {})
    def fake_single(sym, rng=None):
        if sym == "^TNX":
            return {"price": 42.3, "prev": 42.0, "chg": 0.7, "chg_pt": 0.3, "source": "yfinance"}
        return None
    monkeypatch.setattr(df, "_fetch_yf_single", fake_single)
    df._cache_clear("global_markets") if hasattr(df, "_cache_clear") else None
    g = df.fetch_global_markets(force=True)
    assert abs(g["us10y"]["price"] - 4.23) < 0.001  # 42.3 / 10
