"""故障通知雙通道（Telegram 優先、網頁第二）與上櫃日行情抓取重試（2026-10-21）。

使用者要求：故障訊息優先 Telegram 傳、網頁通知中心第二；並修正「上櫃日行情抓取失敗」。
  - send_alert：先送 Telegram、再寫網頁通知中心；任一端失敗都不影響另一端。
  - _fetch_tpex_list：暫時性失敗（逾時/5xx）會退避重試，多半重試即通；連 3 次才回空。
"""
import types


def test_send_alert_telegram_first_then_web(monkeypatch):
    import telegram_bot as tb
    sent = []
    monkeypatch.setattr(tb, "_load_subscribers", lambda: {"admin": ["111"]})
    monkeypatch.setattr(tb, "send_message", lambda cid, msg: sent.append((cid, msg)))
    import state_store
    events = []
    fake = types.SimpleNamespace(add_event=lambda kind, title, body="", ticker="": events.append((kind, title, body)))
    monkeypatch.setattr(state_store, "store", fake)
    tb.send_alert("上櫃日行情抓取失敗，已沿用前次清單 942 檔", level="warning")
    # Telegram 先送到站主
    assert sent and sent[0][0] == "111" and "上櫃日行情抓取失敗" in sent[0][1]
    # 網頁通知中心也有一筆（kind=system）
    assert events and events[0][0] == "system" and "上櫃日行情抓取失敗" in events[0][2]


def test_send_alert_web_still_records_when_telegram_fails(monkeypatch):
    import telegram_bot as tb
    def boom(*a, **k):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(tb, "_load_subscribers", boom)        # Telegram 整段失敗
    import state_store
    events = []
    fake = types.SimpleNamespace(add_event=lambda kind, title, body="", ticker="": events.append((kind, title, body)))
    monkeypatch.setattr(state_store, "store", fake)
    tb.send_alert("測試故障", level="error")                  # 不應拋出例外
    assert events and events[0][0] == "system" and events[0][2] == "測試故障"


def test_tpex_retries_then_succeeds(monkeypatch):
    import stock_universe as su
    import requests, time
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class R:
        def __init__(self, code, payload=None):
            self.status_code = code; self._p = payload or []; self.text = ""
        def json(self): return self._p
    row = [{"SecuritiesCompanyCode": "6488", "CompanyName": "環球晶", "Close": "500",
            "TradingShares": "800000", "Change": "1.0"}]

    def fake_get(url, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("timeout")
        return R(200, row)
    monkeypatch.setattr(requests, "get", fake_get)
    out = su._fetch_tpex_list()
    assert calls["n"] == 3 and len(out) == 1 and out[0]["code"] == "6488"


def test_tpex_returns_empty_after_3_failures(monkeypatch):
    import stock_universe as su
    import requests, time
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class R:
        status_code = 503
        text = "busy"
        def json(self): return []

    def fake_get(url, **k):
        calls["n"] += 1
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    out = su._fetch_tpex_list()
    assert calls["n"] == 3 and out == []
