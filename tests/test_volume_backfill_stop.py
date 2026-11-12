"""官方成交量回補：端點回非 JSON 即停（2026-11-12 系統檢查以線上日誌確認）。

線上證據：official volume backfill 5013 每個月份都「Expecting value: line 1 column 1」——
上櫃 st43 端點回非 JSON（失效或被擋），原本仍對剩下每個月份重打。
"""
import requests

import app


class _R:
    def __init__(self, payload=None, bad=False):
        self.status_code = 200
        self.headers = {"Content-Type": "text/html"}
        self._p, self._bad = payload, bad

    def json(self):
        if self._bad:
            raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        return self._p


def _run(monkeypatch, responses):
    calls = []
    it = iter(responses)
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1) or next(it))
    import state_store
    saved = []
    monkeypatch.setattr(state_store.store, "upsert_official_volumes", lambda rows: saved.extend(rows), raising=False)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    app._backfill_official_volume("5013", True, months=4)
    return calls, saved


def test_stops_after_first_non_json(monkeypatch):
    calls, _ = _run(monkeypatch, [_R(bad=True)] * 4)
    assert len(calls) == 1                     # 端點失效：不再對其餘 3 個月重打


def test_bad_row_value_does_not_abort_other_months(monkeypatch):
    bad_row = _R({"aaData": [["114/11/03", "not-a-number"]]})          # 單筆數字格式異常
    good = _R({"aaData": [["114/10/01", "1,234"]]})
    calls, saved = _run(monkeypatch, [bad_row, good, good, good])
    assert len(calls) == 4                     # 單筆壞資料只略過該月，仍試其他月份
