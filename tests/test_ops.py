"""Phase 1 維運（2026-11-04）：健檢主動告警 + 備份還原驗證。"""
import time

import app
import health
import shadow
import state_store


class _Mem:
    def __init__(self): self.box = {}
    def read_meta(self, k): return ("ok", self.box[k]) if k in self.box else ("missing", None)
    def get_meta(self, k, default=None): return self.box.get(k, default)
    def set_meta(self, k, v): self.box[k] = v; return True


def test_health_watch_alert_throttle_and_recover(monkeypatch):
    monkeypatch.setattr(state_store, "store", _Mem())
    alerts = []
    monkeypatch.setattr(app, "_notify_owner", lambda m, l="info": alerts.append((l, m)))
    monkeypatch.setattr(health, "run_health", lambda: {
        "verdict": "fail", "counts": {"fail": 2},
        "sections": [{"title": "基礎", "items": [{"name": "資料庫", "status": "fail", "detail": "連不上"}]}]})
    r = app.job_health_watch()
    assert r["alerted"] is True and len(alerts) == 1 and alerts[0][0] == "error"   # 新異常→立即告警
    r = app.job_health_watch()
    assert r["alerted"] is False and len(alerts) == 1                               # 持續異常、未過節流→不重發
    monkeypatch.setattr(health, "run_health", lambda: {"verdict": "ok", "counts": {"fail": 0}, "sections": []})
    r = app.job_health_watch()
    assert r["recovered"] is True and len(alerts) == 2 and alerts[1][0] == "info"   # 恢復→通知一次


def test_verify_backup_roundtrip():
    shadow.ensure_table()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = [{"bar_date": f"2026-10-1{i}", "ticker": f"888{i}.TW", "code": f"888{i}", "name": f"T{i}",
             "kind": "candidate", "direction": "buy", "score": 80, "entry": 100, "stop": 95,
             "tp1": 110, "tp2": 115, "tp3": 125, "size_cat": "中型股", "sector": "半導體業",
             "created_at": now, "features": {"f1": 1.0}, "model_prob": None} for i in (1, 2)]
    shadow._insert(rows)
    out = shadow.verify_backup()
    assert out["ok"] and out["match"] is True
    assert out["rows_db"] == out["rows_csv"] == out["rows_restored"] >= 2
