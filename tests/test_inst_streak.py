import pytest
import app as app_module
from state_store import store


@pytest.fixture()
def client(monkeypatch):
    app_module.app.config["TESTING"] = True
    app_module._RATE.clear()
    app_module._INST_STREAK_CACHE["v"] = None
    with store._conn() as conn:
        conn.execute("DELETE FROM inst_daily")
    import stock_universe
    uni = [{"code": c, "name": c, "close": 50.0, "change_pct": 1.0, "sector": "半導體", "is_etf": False}
           for c in ("AAA", "BBB", "CCC")] + [{"code": "ETF1", "name": "ETF", "close": 10.0, "change_pct": 0, "sector": "ETF", "is_etf": True}]
    monkeypatch.setattr(stock_universe, "build_full_universe", lambda *a, **k: uni)
    return app_module.app.test_client()


def _day(d, **net):
    store.upsert_inst_daily(d, {c: {"name": c, "foreign_net": f, "trust_net": t, "total_net": f + t}
                                for c, (f, t) in net.items()})


def test_streak_counts_and_breaks(client):
    # 由舊到新：AAA 外資連買 3 天；BBB 最新一天轉賣；CCC 中間斷一天；ETF 應被排除
    _day("2026-09-30", AAA=(100, 0), BBB=(50, 0), CCC=(10, 0), ETF1=(999, 0))
    _day("2026-10-01", AAA=(200, 0), BBB=(60, 0), CCC=(-5, 0), ETF1=(999, 0))
    _day("2026-10-02", AAA=(300, 5), BBB=(-70, 0), CCC=(20, 0), ETF1=(999, 0))
    j = client.get("/api/market/inst-streak").get_json()
    assert j["days"] >= 3 and j["latest"] == "2026-10-02"
    buy = {r["code"]: r for r in j["foreign_buy"]}
    assert "AAA" in buy and buy["AAA"]["f_streak"] == 3 and buy["AAA"]["f_cum"] == 600
    assert "BBB" not in buy                      # 最新一天轉賣，連買中斷
    assert "CCC" not in buy                      # 只連買 1 天（<2）不列入
    assert "ETF1" not in buy                     # ETF 排除
    assert j["foreign_sell"] == []               # BBB 只連賣 1 天


def test_missing_day_counts_as_break(client):
    _day("2026-10-05", AAA=(100, 0))
    _day("2026-10-06", BBB=(100, 0))             # AAA 這天沒有資料 → 視為 0 → 連買中斷
    j = client.get("/api/market/inst-streak").get_json()
    assert all(r["code"] != "AAA" for r in j["foreign_buy"])


def test_streak_is_cached(client, monkeypatch):
    _day("2026-10-05", AAA=(100, 0))
    a = client.get("/api/market/inst-streak").get_json()
    _day("2026-10-06", AAA=(100, 0))               # 快取期間內新增資料不會立刻反映
    b = client.get("/api/market/inst-streak").get_json()
    assert a["latest"] == b["latest"]
