"""2026-10-15：openapi.twse 從 Render 常逾時，上市日行情改用 www.twse.com.tw 報表端點備援。
以假的報表 JSON 驗證 fields 動態定位、漲跌(+/-) HTML 解析、量/價換算與日期轉換。"""
import stock_universe as su


class _Resp:
    status_code = 200
    def __init__(self, d): self._d = d
    def json(self): return self._d


def test_report_fallback_parses(monkeypatch):
    payload = {
        "stat": "OK", "date": "20261015",
        "fields": ["證券代號", "證券名稱", "成交股數", "成交金額", "開盤價", "最高價",
                   "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"],
        "data": [
            ["2330", "台積電", "25,000,000", "x", "1000", "1015", "995", "1010",
             "<p style='color:red'>+</p>", "5.00"],
            ["2317", "鴻海", "10,000,000", "x", "205", "206", "200", "201",
             "<p style='color:green'>-</p>", "3.00"],
            ["0050", "元大台灣50", "1,000,000", "x", "190", "191", "189", "190",
             "<p> </p>", "0.00"],
        ],
    }
    monkeypatch.setattr(su.requests, "get", lambda *a, **k: _Resp(payload))
    rows = su._twse_list_from_report()
    by = {r["code"]: r for r in rows}
    assert by["2330"]["close"] == 1010 and by["2330"]["change"] == 5.0
    assert by["2330"]["volume_lots"] == 25000 and by["2330"]["quote_date"] == "2026-10-15"
    assert by["2330"]["market"] == "TSE" and by["2330"]["ticker"] == "2330.TW"
    assert by["2317"]["change"] == -3.0 and by["2317"]["change_pct"] < 0   # 綠=跌，change 為負
    assert by["0050"]["change"] == 0.0


def test_report_fallback_bad_structure_returns_empty(monkeypatch):
    monkeypatch.setattr(su.requests, "get", lambda *a, **k: _Resp({"stat": "OK", "fields": ["亂七八糟"], "data": [[1]]}))
    assert su._twse_list_from_report() == []     # 欄位不符 → 回空（不臆測）


def test_fetch_twse_list_falls_back(monkeypatch):
    monkeypatch.setattr(su, "_twse_list_openapi", lambda: [])       # openapi 掛了
    monkeypatch.setattr(su, "_twse_list_from_report", lambda: [{"code": "2330"}])
    assert su._fetch_twse_list() == [{"code": "2330"}]
