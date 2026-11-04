"""事件濾網（2026-11-04）：近日除權息、重大訊息風險、fail-soft、TTL 快取。"""
import datetime as dt

import event_filter as ef


def _reset(monkeypatch):
    monkeypatch.setattr(ef, "_cache", {})


def test_imminent_exdiv_window(monkeypatch):
    _reset(monkeypatch)
    today = ef._today_iso()
    far = (dt.date.fromisoformat(today) + dt.timedelta(days=30)).isoformat()
    import calendar_events
    monkeypatch.setattr(calendar_events, "get_ex_dividend",
                        lambda days=21: {"items": [{"code": "2330", "date": today},
                                                   {"code": "9999", "date": far}]})
    s = ef.imminent_exdiv_codes(within_days=2)
    assert "2330" in s and "9999" not in s


def test_material_news_codes(monkeypatch):
    _reset(monkeypatch)
    import fundamentals
    monkeypatch.setattr(fundamentals, "fetch_material_news_risk_map",
                        lambda: {"2317": [{"subject": "訴訟"}], "1111": []})
    s = ef.material_news_codes()
    assert "2317" in s and "1111" not in s


def test_check_combines(monkeypatch):
    _reset(monkeypatch)
    import calendar_events, fundamentals
    today = ef._today_iso()
    monkeypatch.setattr(calendar_events, "get_ex_dividend", lambda days=21: {"items": [{"code": "2330", "date": today}]})
    monkeypatch.setattr(fundamentals, "fetch_material_news_risk_map", lambda: {"2317": [{"s": 1}]})
    assert ef.check("2330") == {"exdiv": True, "news": False}
    assert ef.check("2317") == {"exdiv": False, "news": True}
    assert ef.check("0050") == {"exdiv": False, "news": False}


def test_failsoft_on_source_error(monkeypatch):
    _reset(monkeypatch)
    import calendar_events

    def _boom(*a, **k):
        raise RuntimeError("source down")
    monkeypatch.setattr(calendar_events, "get_ex_dividend", _boom)
    assert ef.imminent_exdiv_codes() == set()      # 抓取失敗 → 空集合、不崩、不擋所有訊號


def test_ttl_cache_builds_once(monkeypatch):
    _reset(monkeypatch)
    import calendar_events
    calls = {"n": 0}

    def _g(days=21):
        calls["n"] += 1
        return {"items": []}
    monkeypatch.setattr(calendar_events, "get_ex_dividend", _g)
    ef.imminent_exdiv_codes(within_days=2)
    ef.imminent_exdiv_codes(within_days=2)
    assert calls["n"] == 1      # TTL 內只建一次
