"""大盤三大法人官方金額解析（BFI82U，2026-10-21）。

使用者要求「大盤三大法人買賣超除了張數再加官方金額」。BFI82U 本就逐法人列出買賣差額金額，
這裡驗證三大法人各自金額被正確彙整（外資＝外資及陸資＋外資自營商、自營＝自行買賣＋避險），
且既有的 net_buy_twd（外資及陸資不含自營商）維持不變、不影響既有熔斷/報告邏輯。
"""


def test_bfi82u_parses_all_three_amounts(monkeypatch):
    import data_fetcher as df
    import requests
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)   # 強制不吃快取
    monkeypatch.setattr(df, "_cache_set", lambda k, v: v)

    class R:
        status_code = 200
        def json(self):
            return {"stat": "OK", "date": "20261021", "data": [
                ["自營商(自行買賣)", "9", "9", "100000000"],
                ["自營商(避險)", "9", "9", "50000000"],
                ["投信", "9", "9", "300000000"],
                ["外資及陸資(不含外資自營商)", "9", "9", "2,000,000,000"],
                ["外資自營商", "9", "9", "-30000000"],
                ["合計", "9", "9", "2420000000"],
            ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    out = df.fetch_foreign_total_flow()
    a = out["amounts"]
    assert a["foreign"] == 2_000_000_000 - 30_000_000      # 外資及陸資 ＋ 外資自營商
    assert a["trust"] == 300_000_000
    assert a["dealer"] == 100_000_000 + 50_000_000          # 自行買賣 ＋ 避險
    assert a["total"] == 2_420_000_000                      # 官方合計列
    assert out["net_buy_twd"] == 2_000_000_000              # 既有欄位不變（外資及陸資不含自營商）


def test_bfi82u_missing_rows_degrade_to_none(monkeypatch):
    import data_fetcher as df
    import requests
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(df, "_cache_set", lambda k, v: v)

    class R:
        status_code = 200
        def json(self):
            # 只有外資及陸資一列（其餘法人缺）→ foreign 有值、trust/dealer 為 None，不編造
            return {"stat": "OK", "date": "20261021", "data": [
                ["外資及陸資(不含外資自營商)", "9", "9", "1000000000"],
            ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    out = df.fetch_foreign_total_flow()
    a = out["amounts"]
    assert a["foreign"] == 1_000_000_000 and a["trust"] is None and a["dealer"] is None
