"""2026-10-16 法人資金流向板塊圖：/api/market/inst-flow-sectors 依產業板塊加總
三大法人（T86）淨買賣超。以假資料驗證板塊加總、自營商＝合計−外資−投信、
分類不明或樣本太少不納入。"""
import pytest

import app as app_module


@pytest.fixture()
def client():
    return app_module.app.test_client()


def test_inst_flow_sectors_aggregates_by_sector(client, monkeypatch):
    import state_store
    import stock_universe as su

    dates = ["2026-10-15", "2026-10-14"]
    rows = [
        # 半導體業 3 檔（達門檻）
        {"trade_date": "2026-10-15", "code": "2330", "foreign_net": 1000, "trust_net": 100, "total_net": 1200},
        {"trade_date": "2026-10-15", "code": "2454", "foreign_net": 500, "trust_net": 50, "total_net": 600},
        {"trade_date": "2026-10-14", "code": "2379", "foreign_net": -200, "trust_net": 10, "total_net": -150},
        # 航運業只有 1 檔（< 3，應被濾除）
        {"trade_date": "2026-10-15", "code": "2603", "foreign_net": 9999, "trust_net": 0, "total_net": 9999},
    ]
    sect = {"2330": "半導體業", "2454": "半導體業", "2379": "半導體業", "2603": "航運業"}

    monkeypatch.setattr(state_store.store, "get_inst_dates", lambda limit=60: dates)
    monkeypatch.setattr(state_store.store, "get_inst_since", lambda d: rows)
    monkeypatch.setattr(su, "build_full_universe",
                        lambda *a, **k: [{"code": c, "sector": s} for c, s in sect.items()])
    app_module._INST_SECTOR_CACHE.update(v=None, key=None, t=0)

    d = client.get("/api/market/inst-flow-sectors?days=5").get_json()
    assert d["unit"] == "張" and d["latest_date"] == "2026-10-15"
    secs = {s["sector"]: s for s in d["sectors"]}
    assert "航運業" not in secs, "樣本不足的板塊應被濾除"
    semi = secs["半導體業"]
    assert semi["foreign"] == 1300 and semi["trust"] == 160    # 1000+500-200, 100+50+10
    assert semi["total"] == 1650
    assert semi["dealer"] == semi["total"] - semi["foreign"] - semi["trust"]
    assert semi["n"] == 3


def test_inst_flow_sectors_empty_when_no_data(client, monkeypatch):
    import state_store
    monkeypatch.setattr(state_store.store, "get_inst_dates", lambda limit=60: [])
    app_module._INST_SECTOR_CACHE.update(v=None, key=None, t=0)
    d = client.get("/api/market/inst-flow-sectors").get_json()
    assert d["sectors"] == [] and d["latest_date"] is None
