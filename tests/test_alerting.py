"""主動 TG 告警完善度測試（2026-11-05）：
- 排程工作崩潰 → 全域 listener 發告警，且同類 30 分鐘內去重不洗版。
- _alert_once 去重（claim_notice 回 False 時不重發）。
- 交易掃描時段非預定重啟 → 應通知；預定重啟／非時段／假日 → 不通知。
"""
import types
from datetime import datetime, timezone, timedelta

import pytest

import app as app_module

_TPE = timezone(timedelta(hours=8))


class _FakeStore:
    def __init__(self, allow=True):
        self._allow = allow
        self.claims = []

    def claim_notice(self, key, ttl_sec=600):
        self.claims.append((key, ttl_sec))
        return self._allow            # True=可發，False=已於 ttl 內發過


@pytest.fixture()
def cap_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "_notify_owner", lambda msg, level="info": sent.append((level, msg)))
    return sent


def _patch_store(monkeypatch, store):
    import state_store
    monkeypatch.setattr(state_store, "store", store)


# ── _alert_once 去重 ──
def test_alert_once_sends_when_claim_ok(monkeypatch, cap_notify):
    _patch_store(monkeypatch, _FakeStore(allow=True))
    assert app_module._alert_once("k1", "訊息", "error", 1800) is True
    assert len(cap_notify) == 1 and cap_notify[0][0] == "error"


def test_alert_once_suppressed_when_claim_taken(monkeypatch, cap_notify):
    _patch_store(monkeypatch, _FakeStore(allow=False))
    assert app_module._alert_once("k1", "訊息", "error", 1800) is False
    assert cap_notify == []            # 去重期間不重發


# ── 排程錯誤 listener ──
def test_on_job_error_alerts(monkeypatch, cap_notify):
    store = _FakeStore(allow=True)
    _patch_store(monkeypatch, store)
    ev = types.SimpleNamespace(job_id="daily_scan", exception=RuntimeError("boom"))
    app_module._on_job_error(ev)
    assert len(cap_notify) == 1
    lvl, msg = cap_notify[0]
    assert lvl == "error"
    assert "daily_scan" in msg and "RuntimeError" in msg and "boom" in msg
    # 去重 key 應含工作 id 與錯誤型別
    assert any("joberr:daily_scan:RuntimeError" in k for k, _ in store.claims)


def test_on_job_error_ignores_success_event(monkeypatch, cap_notify):
    _patch_store(monkeypatch, _FakeStore(allow=True))
    ev = types.SimpleNamespace(job_id="daily_scan", exception=None)
    app_module._on_job_error(ev)
    assert cap_notify == []            # 沒有例外 → 不告警


def test_on_job_error_dedups_same_type(monkeypatch, cap_notify):
    """同一工作同型別錯誤連兩次：claim_notice 第一次 True、第二次 False → 只發一則。"""
    class _OnceStore:
        def __init__(self):
            self.seen = set()
        def claim_notice(self, key, ttl_sec=600):
            if key in self.seen:
                return False
            self.seen.add(key)
            return True
    _patch_store(monkeypatch, _OnceStore())
    ev = types.SimpleNamespace(job_id="intraday_check", exception=ValueError("x"))
    app_module._on_job_error(ev)
    app_module._on_job_error(ev)
    assert len(cap_notify) == 1


# ── 非預定重啟判斷（純函式）──
def _now(h, m, weekday=2):
    # 找一個指定星期幾的日期（weekday: 0=一 ... 6=日）
    d = datetime(2026, 11, 4, h, m, tzinfo=_TPE)   # 2026-11-04 是週三(2)
    shift = weekday - d.weekday()
    return d + timedelta(days=shift)


def test_restart_notice_due_in_premarket_scan_window():
    assert app_module._restart_notice_due(_now(8, 30), 0, 1000.0) is True


def test_restart_notice_due_in_postmarket_scan_window():
    assert app_module._restart_notice_due(_now(16, 30), 0, 1000.0) is True


def test_restart_notice_not_due_outside_window():
    assert app_module._restart_notice_due(_now(11, 0), 0, 1000.0) is False


def test_restart_notice_not_due_on_weekend():
    assert app_module._restart_notice_due(_now(16, 30, weekday=5), 0, 1000.0) is False


def test_restart_notice_not_due_when_planned():
    now_ts = 1000.0
    planned = now_ts - 10          # 剛剛（10 秒前）的預定重啟
    assert app_module._restart_notice_due(_now(16, 20), planned, now_ts) is False


def test_restart_notice_due_when_planned_is_stale():
    now_ts = 1000.0
    planned = now_ts - 9999        # 很久以前的預定重啟旗標，不應抑制這次
    assert app_module._restart_notice_due(_now(16, 20), planned, now_ts) is True
