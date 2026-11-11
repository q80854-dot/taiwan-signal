"""LIVE13（第三階段審查）：異常殖利率隔離（顯示層）。

台股現金殖利率 >15% 多為資料問題（除權息基準/面額/拆分/幣別/單位）。顯示層隔離：
標 yield_anomaly、不當高殖利率、不進「高殖利率」篩選、不排到榜首。
計分面（_valuation→fund_adjustment）屬 risk:champion，本輪不改。
"""
import app


def _radar(monkeypatch, rev, val):
    import fundamentals as fd
    from state_store import store
    monkeypatch.setattr(fd, "fetch_monthly_revenue_map", lambda: rev)
    monkeypatch.setattr(fd, "fetch_valuation_map", lambda: val)
    monkeypatch.setattr(store, "get_recent_signals", lambda limit=60, days_back=7: [])
    c = app.app.test_client()
    return c.get("/api/fundamentals_radar?kind=all").get_json()


def test_anomalous_yield_flagged_not_dropped(monkeypatch):
    rev = {"1101": {"name": "異常股", "sector_name": "水泥", "period": "11411",
                    "yoy_pct": 30, "cum_yoy_pct": 20, "mom_pct": 2, "revenue": 500000, "revenue_ly": 300000}}
    val = {"1101": {"pe": 12, "yield_pct": 148.45, "pb": 1.2}}
    d = _radar(monkeypatch, rev, val)
    row = next(r for r in d["rows"] if r["code"] == "1101")
    assert row["yield_anomaly"] is True
    assert row["yield"] == 148.45            # 原值保留顯示
    assert d["yield_anomaly_pct"] == 15.0


def test_normal_yield_not_flagged(monkeypatch):
    rev = {"2412": {"name": "正常股", "sector_name": "電信", "period": "11411",
                    "yoy_pct": 25, "cum_yoy_pct": 15, "mom_pct": 1, "revenue": 500000, "revenue_ly": 300000}}
    val = {"2412": {"pe": 15, "yield_pct": 4.8, "pb": 1.5}}
    d = _radar(monkeypatch, rev, val)
    row = next(r for r in d["rows"] if r["code"] == "2412")
    assert row["yield_anomaly"] is False and row["yield"] == 4.8
