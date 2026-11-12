"""外資持股比率重抓風暴修正（2026-11-12 系統檢查以線上日誌確認）。

線上證據：freshness 每約 2 分鐘記一次「資料新鮮度補抓：['foreign_holding']」，且
從未出現任何失敗原因——_build_foreign_holding_map 對 HTTP 非 200／stat 非 OK／無資料
一律 continue 不留紀錄，一次最多打 8 個證交所請求；/api/freshness 只要有人開網頁、
有資料落後就每分鐘觸發補抓。來源永遠抓不到 → 一小時數百次請求。
"""
import company_info as ci
import freshness


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._p = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._p


_OK = {"stat": "OK", "date": "20261111",
       "fields": ["證券代號", "證券名稱", "全體外資及陸資持股比率", "全體外資及陸資投資上限比率"],
       "data": [["2330", "台積電", "72.10", "100.00"], ["2317", "鴻海", "40.50", "100.00"]]}


def test_both_endpoints_broken_stops_after_two_requests_and_logs_reason(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(ci.requests, "get", lambda url, **k: calls.append(url) or _Resp(404))
    with caplog.at_level("WARNING"):
        m = ci._build_foreign_holding_map()
    assert m == {}                                       # 取不到 → 空，不以推估值替代
    assert len(calls) == 2                               # 兩端點皆失效即停，不再往前打 8 天
    assert "HTTP 404" in caplog.text and "MI_QFIIS" in caplog.text   # 失敗原因留紀錄


def test_current_rwd_endpoint_tried_first_and_walks_back_on_no_data(monkeypatch):
    calls = []

    def fake(url, **k):
        calls.append(url)
        if "/rwd/" in url and len(calls) == 1:
            return _Resp(200, {"stat": "很抱歉，沒有符合條件的資料!"})   # 今日尚未公布
        if "/rwd/" in url:
            return _Resp(200, _OK)
        return _Resp(200, {"stat": "很抱歉，沒有符合條件的資料!"})
    monkeypatch.setattr(ci.requests, "get", fake)
    m = ci._build_foreign_holding_map()
    assert "/rwd/" in calls[0]                           # 現行端點優先
    assert m["2330"]["ratio"] == 72.10 and m["2330"]["date"] == "20261111"


def test_legacy_endpoint_used_when_rwd_broken(monkeypatch):
    monkeypatch.setattr(ci.requests, "get",
                        lambda url, **k: _Resp(500) if "/rwd/" in url else _Resp(200, _OK))
    m = ci._build_foreign_holding_map()
    assert m["2317"]["ratio"] == 40.50                   # 舊端點備援仍可用


def test_non_json_response_marks_endpoint_broken(monkeypatch):
    calls = []
    monkeypatch.setattr(ci.requests, "get", lambda url, **k: calls.append(url) or _Resp(200, bad_json=True))
    assert ci._build_foreign_holding_map() == {}
    assert len(calls) == 2


def test_freshness_cooldown_prevents_hammering(monkeypatch):
    freshness._last_try.clear()
    monkeypatch.setattr(freshness, "status",
                        lambda now=None: {"items": [{"key": "foreign_holding", "status": "late"}]})
    tries = []
    monkeypatch.setattr(ci, "get_foreign_holding", lambda code: tries.append(code) or None)
    import protect
    monkeypatch.setattr(protect, "clear_cache", lambda: None)
    freshness.job_poll()
    freshness.job_poll()
    freshness.job_poll()
    assert tries == ["2330"]                             # 冷卻期內只重抓一次，不再每分鐘重打

    freshness._last_try["foreign_holding"] -= freshness._RETRY_COOLDOWN_SEC + 1
    freshness.job_poll()
    assert len(tries) == 2                               # 冷卻過後才再試
