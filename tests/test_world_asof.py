"""world 看板逐項來源時間 asof（LIVE08 資料契約缺口補齊）。

審查 LIVE08 指出「國際/FX tile 的來源時間後端未提供」——全板只有快取時間 at，
無法知道個別標的報價實際是哪一天的資料（各市場收盤時間不同、單檔可能落後）。
補齊：抓取端記每項資料自身最後一根日線的交易日（asof，日粒度），取不到＝None，
不以全板時間冒充逐項來源時間、不回填、不假造；前端 tile 顯示「資料日 MM/DD」。
"""
import os

import pandas as pd
import pytest

import data_fetcher as df


@pytest.fixture(autouse=True)
def _nocache(monkeypatch):
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)   # 不吃快取
    monkeypatch.setattr(df, "_cache_set", lambda k, v: v)


def _hist(days=5):
    idx = pd.date_range("2026-11-05", periods=days, freq="D")
    return pd.DataFrame({"Close": [100.0 + i for i in range(days)],
                         "Low":   [99.0] * days,
                         "High":  [101.0 + i for i in range(days)]}, index=idx)


class _FakeTicker:
    def __init__(self, sym):
        self.sym = sym

    def history(self, **kw):
        return _hist()


def test_fetch_yf_board_and_single_carry_asof(monkeypatch):
    monkeypatch.setattr(df, "YFINANCE_OK", True)
    monkeypatch.setattr(df, "yf", type("Y", (), {"Ticker": _FakeTicker}))
    r = df._fetch_yf_board("^GSPC")
    assert r["price"] == 104.0 and r["asof"] == "2026-11-09"     # 最後一根 bar 的交易日
    r2 = df._fetch_yf_single("^GSPC")
    assert r2["asof"] == "2026-11-09"


def test_world_board_items_carry_asof_or_none(monkeypatch):
    def fake(sym, rng=None):
        if sym == "^VIX":
            return None                                           # 抓不到
        return {"price": 100.0, "prev": 99.0, "chg": 1.01, "chg_pt": 1.0,
                "source": "yfinance", "asof": "2026-11-10"}
    monkeypatch.setattr(df, "_fetch_yf_single", fake)
    monkeypatch.setattr(df, "_fetch_yf_board", fake)
    b = df.fetch_world_board(force=True)
    assert b["futures"][0]["ok"] is True and b["futures"][0]["asof"] == "2026-11-10"
    vix = next(x for x in b["futures"] if x["key"] == "vix")
    assert vix["ok"] is False and vix["asof"] is None             # 取不到＝None，不冒充


def test_dashboard_shows_per_item_asof():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "templates", "dashboard.html")
    s = open(p, encoding="utf-8").read()
    assert "資料日" in s and "it.asof" in s                        # 首頁 wcard 逐項資料日
    assert "_wN(x,'日線')" in s and "_wN(x,'24h')" in s            # 總覽 tile note 帶 asof
