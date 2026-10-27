"""首頁全球市場看板 fetch_world_board / /api/world（2026-10-27）。

使用者要求首頁像富途那樣看指數期貨／全球指數／商品·外匯·債券。全部走 yfinance
（_fetch_yf_single），抓不到就 ok=False、price=None（顯示暫無、不編造）。這裡用 mock
驗證：五組都在、順序正確、抓到/抓不到各自標記正確、/api/world 回傳結構。
"""
import pytest

import data_fetcher as df
import app as app_module


@pytest.fixture(autouse=True)
def _nocache(monkeypatch):
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)   # 不吃快取
    monkeypatch.setattr(df, "_cache_set", lambda k, v: v)


def test_world_board_structure_and_ok_flags(monkeypatch):
    # 只有部分符號抓得到：^VIX 回 None，其餘回固定值
    def fake(sym, rng=None):
        if sym == "^VIX":
            return None
        return {"price": 100.0, "prev": 99.0, "chg": 1.01, "chg_pt": 1.0, "source": "yfinance"}
    monkeypatch.setattr(df, "_fetch_yf_single", fake)
    b = df.fetch_world_board(force=True)
    for g in ("futures", "indices", "commodities", "fx", "bonds"):
        assert g in b and isinstance(b[g], list) and b[g]
    assert "at" in b
    # 期貨組順序第一個是道瓊期貨、且有抓到值
    assert b["futures"][0]["key"] == "dow_fut" and b["futures"][0]["ok"] is True and b["futures"][0]["price"] == 100.0
    # VIX 抓不到 → ok False、price None（不編造）
    vix = next(x for x in b["futures"] if x["key"] == "vix")
    assert vix["ok"] is False and vix["price"] is None


def test_world_board_all_fail_graceful(monkeypatch):
    monkeypatch.setattr(df, "_fetch_yf_single", lambda sym, rng=None: None)
    b = df.fetch_world_board(force=True)
    assert all(not it["ok"] and it["price"] is None for g in ("futures", "indices", "commodities", "fx", "bonds") for it in b[g])


def test_api_world_endpoint(monkeypatch):
    monkeypatch.setattr(df, "_fetch_yf_single",
                        lambda sym, rng=None: {"price": 50.0, "prev": 49.0, "chg": 2.04, "chg_pt": 1.0, "source": "yfinance"})
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    j = c.get("/api/world").get_json()
    assert "futures" in j and "bonds" in j and j["indices"][0]["ok"] is True
