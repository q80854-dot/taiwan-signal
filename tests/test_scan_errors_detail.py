"""掃描錯誤逐類別明細（LIVE14 缺口補齊）。

- scanner 按類型（逾時/例外類名）與市場（上市/上櫃）聚計、記受影響標的（上限 30）。
- scan_history 存 errors_detail JSON（無錯誤存 NULL、不存空殼）；讀取還原為 dict。
- 健檢：有明細時逐類別呈現；舊列無明細保留誠實措辭（標明 2026-11-12 起記錄）；錯誤>0 仍 warn。
"""
import os
import uuid

import health
from state_store import store


def test_scan_history_errors_detail_roundtrip():
    rid = "scan-test-" + uuid.uuid4().hex[:8]
    ed = {"timeout": 2, "exception": 1, "by_exc": {"KeyError": 1},
          "by_market": {"上市": 2, "上櫃": 1}, "tickers": ["1111.TW", "2222.TW", "3333.TWO"]}
    store.save_scan_history({"scanned": 10, "errors": 3, "run_id": rid, "errors_detail": ed})
    row = (store.get_scan_history(1) or [{}])[0]
    assert row.get("run_id") == rid and row.get("errors_detail") == ed      # JSON 往返

    store.save_scan_history({"scanned": 10, "errors": 0})                   # 無錯誤 → NULL
    row2 = (store.get_scan_history(1) or [{}])[0]
    assert row2.get("errors_detail") is None


def test_scanner_records_categories():
    s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scanner.py"),
             encoding="utf-8").read()
    assert "_rec_err(\"timeout\", ticker)" in s
    assert "_rec_err(\"exception\", ticker, e)" in s
    assert "type(exc).__name__" in s                                        # 例外類名聚計
    assert '".TWO"' in s[s.index("def _rec_err"):s.index("def _rec_err") + 600]   # 市場別
    assert '"errors_detail": err_detail if error_count else None' in s      # 無錯誤不存空殼


def test_health_shows_detail_or_honest_legacy(monkeypatch):
    base = {"scan_date": "2026-11-12", "scanned": 100, "signals_found": 1,
            "signals_sent": 1, "duration_min": 5}
    ed = {"timeout": 1, "exception": 2, "by_exc": {"ValueError": 2},
          "by_market": {"上市": 3}, "tickers": ["9999.TW"]}
    monkeypatch.setattr(store, "get_scan_history",
                        lambda n=3: [{**base, "errors": 3, "errors_detail": ed}])
    items = health.check_engine()
    scan = [i for i in items if i["name"] == "最近一次掃描"][0]
    assert scan["status"] == "warn"
    assert "逐類別" in scan["detail"] and "逾時 1" in scan["detail"]
    assert "ValueError×2" in scan["detail"] and "上市 3" in scan["detail"] and "9999.TW" in scan["detail"]

    # 舊列（無明細）→ 誠實標明無逐類別、不假造
    monkeypatch.setattr(store, "get_scan_history",
                        lambda n=3: [{**base, "errors": 3, "errors_detail": None}])
    scan2 = [i for i in health.check_engine() if i["name"] == "最近一次掃描"][0]
    assert scan2["status"] == "warn" and "無逐類別明細" in scan2["detail"]

    # 無錯誤 → ok、無警語
    monkeypatch.setattr(store, "get_scan_history",
                        lambda n=3: [{**base, "errors": 0, "errors_detail": None}])
    scan3 = [i for i in health.check_engine() if i["name"] == "最近一次掃描"][0]
    assert scan3["status"] == "ok" and "⚠" not in scan3["detail"]
