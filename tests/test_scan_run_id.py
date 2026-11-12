"""scan_run_id 綁定（LIVE07 完整版，資料契約缺口補齊）。

- scanner 每次掃描產生唯一 run_id，寫入本批每筆訊號（記憶體＋raw_json）、scan_history、
  last_scan_audit——漏斗/訊號卡按 run_id 確定性綁定，不再靠日期推測。
- 舊列/舊訊號無 run_id＝NULL（誠實「無紀錄」，不回填、不假造）。
- 前端：三源都有 run_id 時按 run_id 綁定；任一缺才退回日期啟發式並標明。
"""
import json
import os
import uuid

from state_store import store


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_scan_history_run_id_roundtrip():
    rid = "scan-test-" + uuid.uuid4().hex[:8]
    store.save_scan_history({"scanned": 1, "signals_found": 0, "signals_sent": 0,
                             "duration_min": 0.1, "errors": 0, "run_id": rid})
    row = (store.get_scan_history(1) or [{}])[0]
    assert row.get("run_id") == rid


def test_scan_history_run_id_optional_null():
    # 不帶 run_id 也能寫入（防呆），讀回為 None＝誠實無紀錄
    store.save_scan_history({"scanned": 1, "signals_found": 0, "signals_sent": 0,
                             "duration_min": 0.1, "errors": 0})
    row = (store.get_scan_history(1) or [{}])[0]
    assert row.get("run_id") is None


def test_signal_scan_run_id_lifted_from_raw_json():
    rid = "scan-test-" + uuid.uuid4().hex[:8]
    sid = "RUNID_TEST_" + uuid.uuid4().hex[:8]
    sig = {"id": sid, "ticker": "RUNID.TW", "name": "測試", "direction": "buy", "score": 70,
           "current_price": 100.0, "entry_price": 100.0, "stop_loss": 95.0, "tp1": 105.0,
           "scan_run_id": rid}
    try:
        assert store.save_signal(sig)
        rows = [r for r in store.get_recent_signals(limit=100, days_back=2) if r.get("id") == sid]
        assert rows and rows[0].get("scan_run_id") == rid          # raw_json → 讀取時提取
    finally:
        with store._conn() as conn:
            conn.execute("DELETE FROM signals WHERE id=?", (sid,))


def test_scanner_wiring():
    s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scanner.py"),
             encoding="utf-8").read()
    i = s.index("scan_run_id（LIVE07 完整版）")
    assert "uuid" in s[i:i + 600]
    assert '_s["scan_run_id"] = run_id' in s                       # 本批訊號標記
    assert '"run_id":        run_id' in s                          # stats → scan_history
    assert '"run_id": run_id' in s                                 # last_scan_audit meta


def test_frontend_binds_by_run_id_with_date_fallback():
    s = _dash()
    assert "const aRid=a.run_id||null,hRid=h.run_id||null" in s
    assert "scan_run_id)||{}).scan_run_id" in s                    # 卡片側取本批訊號 run_id
    assert "(aRid&&hRid)?aRid===hRid:" in s                        # run_id 優先、日期為備援
    assert "舊資料無 run_id，依日期判斷" in s                        # 備援時誠實標明
