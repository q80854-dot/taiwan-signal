from datetime import datetime

import freshness as fr

NOHOL = lambda d: False


def test_expected_date_before_and_after_publish():
    assert fr.expected_date("16:30", datetime(2026, 10, 8, 16, 0), NOHOL) == "2026-10-07"
    assert fr.expected_date("16:30", datetime(2026, 10, 8, 16, 31), NOHOL) == "2026-10-08"
    assert fr.expected_date("16:30", datetime(2026, 10, 10, 12, 0), NOHOL) == "2026-10-09"     # 週六 → 週五
    assert fr.expected_date("16:30", datetime(2026, 10, 12, 9, 0), NOHOL) == "2026-10-09"      # 週一早上 → 上週五
    hol = lambda d: d.strftime("%Y-%m-%d") == "2026-10-09"
    assert fr.expected_date("16:30", datetime(2026, 10, 10, 12, 0), hol) == "2026-10-08"       # 遇國定假日再往前


def test_status_marks_late_and_ok(monkeypatch):
    monkeypatch.setattr(fr, "_is_holiday", lambda d: False)
    now = datetime(2026, 10, 8, 18, 30)
    dates = {"taifex": "2026-10-07", "inst": "2026-10-08", "intraday": None}
    st = fr.status(now, dates)
    by = {i["key"]: i for i in st["items"]}
    assert by["taifex"]["status"] == "late" and by["taifex"]["expected"] == "2026-10-08"
    assert by["inst"]["status"] == "ok"
    assert by["intraday"]["status"] == "unknown"
    assert by["margin"]["expected"] == "2026-10-07"        # 21:30 才公布，18:30 時應有的是前一天
    assert "taifex" in st["late"] and "taifex=2026-10-07" in st["version"]


def test_job_poll_only_refetches_late(monkeypatch):
    import taifex, data_fetcher
    calls = []
    monkeypatch.setattr(fr, "status", lambda now=None, dates=None: {"items": [
        {"key": "taifex", "status": "late", "expected": "2026-10-08"},
        {"key": "inst", "status": "ok", "expected": "2026-10-08"},
        {"key": "tse_quote", "status": "ok"}, {"key": "otc_quote", "status": "ok"},
        {"key": "foreign", "status": "ok"}, {"key": "margin", "status": "ok"}]})
    monkeypatch.setattr(taifex, "get_taifex", lambda force=False: calls.append("taifex") or {})
    monkeypatch.setattr(taifex, "update_hist", lambda store, data: None)
    monkeypatch.setattr(data_fetcher, "collect_inst_daily", lambda d, s: calls.append("inst") or 0)
    assert fr.job_poll()["did"] == ["taifex"] and calls == ["taifex"]


def test_kick_background_is_throttled(monkeypatch):
    import time as _t
    ran = []
    monkeypatch.setattr(fr, "job_poll", lambda: ran.append(1))
    fr._bg.update(running=False, last=0.0)
    assert fr.kick_background(min_gap=60) is True
    for _ in range(50):
        if not fr._bg["running"]:
            break
        _t.sleep(0.01)
    assert fr.kick_background(min_gap=60) is False      # 一分鐘內不重複
    assert ran == [1]


def test_log_summary_only_logs_changes(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="freshness")
    monkeypatch.setattr(fr, "status", lambda now=None, dates=None: {"items": [
        {"key": "taifex", "name": "期交所", "date": "2026-10-08", "expected": "2026-10-08", "status": "ok"}]})
    fr._last_summary["v"] = None
    fr.log_summary(); fr.log_summary()
    assert sum("資料新鮮度 1/1 最新" in r.message for r in caplog.records) == 1
