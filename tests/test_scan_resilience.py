"""掃描可靠性（2026-11-12 系統檢查以線上日誌確認之根因修正）。

線上證據：2026-11-11 16:30 盤後掃描於 16:40 被一次部署砍掉（第 22/27 批，
"cannot schedule new futures after interpreter shutdown"），當天訊號／影子收集／結算全部遺失；
17:00 watchdog 只發警示、不補跑；影子結算綁在掃描最後一步，於是「已結算 8156」三十多小時不動、
逾期卡單修正（#201）也一直沒被執行。
"""
from datetime import datetime

import app
import scanner as scanner_mod


class _Meta:
    def __init__(self, init=None): self.box = dict(init or {})
    def get_meta(self, k, default=None): return self.box.get(k, default)
    def set_meta(self, k, v): self.box[k] = v; return True


class _FakeScanner:
    def __init__(self, last="", scanning=False):
        self.last_scan_at = last
        self._scanning = scanning
    @property
    def is_scanning(self): return self._scanning


def _patch(monkeypatch, sc, meta=None):
    monkeypatch.setattr(scanner_mod, "scanner", sc)
    import state_store
    m = meta or _Meta()
    monkeypatch.setattr(state_store, "store", m)
    started = []

    class _T:
        def __init__(self, target=None, **k): self.target = target
        def start(self): started.append(self.target)
    monkeypatch.setattr(app.threading, "Thread", _T)
    alerts = []
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "send_alert", lambda msg, lvl="info": alerts.append(msg))
    return m, started, alerts


def test_last_scan_date_converts_utc_to_taipei(monkeypatch):
    _patch(monkeypatch, _FakeScanner("2026-11-10T08:48:40+00:00"))
    assert app._last_scan_tpe_date() == "2026-11-10"
    _patch(monkeypatch, _FakeScanner("2026-11-10T17:00:00+00:00"))     # 台北已跨日
    assert app._last_scan_tpe_date() == "2026-11-11"
    _patch(monkeypatch, _FakeScanner(""))
    assert app._last_scan_tpe_date() == ""


def test_autoretry_starts_scan_and_caps_per_day(monkeypatch):
    m, started, _ = _patch(monkeypatch, _FakeScanner())
    assert app._try_autoretry_scan("2026-11-12", "t") is True
    assert app._try_autoretry_scan("2026-11-12", "t") is True
    assert app._try_autoretry_scan("2026-11-12", "t") is False          # 每日上限 2 次，防崩潰迴圈
    assert len(started) == 2 and m.box["scan_autoretry"]["n"] == 2
    assert app._try_autoretry_scan("2026-11-13", "t") is True           # 隔天重新計數


def test_autoretry_never_double_runs_while_scanning(monkeypatch):
    _, started, _ = _patch(monkeypatch, _FakeScanner(scanning=True))
    assert app._try_autoretry_scan("2026-11-12", "t") is False
    assert not started


def test_watchdog_no_false_alarm_while_scan_still_running(monkeypatch):
    import data_fetcher
    monkeypatch.setattr(data_fetcher, "get_market_session", lambda: {"session": "post"})
    _, started, alerts = _patch(monkeypatch, _FakeScanner("2026-11-10T08:48:40+00:00", scanning=True))
    app.job_scan_watchdog()
    assert not alerts and not started                                    # 慢≠沒跑，不誤報


def test_watchdog_auto_reruns_missed_scan(monkeypatch):
    import data_fetcher
    monkeypatch.setattr(data_fetcher, "get_market_session", lambda: {"session": "post"})
    _, started, alerts = _patch(monkeypatch, _FakeScanner("2026-11-10T08:48:40+00:00"))
    app.job_scan_watchdog()
    assert len(started) == 1 and started[0] is app.job_daily_scan        # 自動補跑
    assert alerts and "已自動補跑" in alerts[0]


def test_boot_catchup_only_in_post_close_window(monkeypatch):
    import data_fetcher
    monkeypatch.setattr(data_fetcher, "get_market_session", lambda: {"session": "post"})

    class _DT(datetime):
        _now = None
        @classmethod
        def now(cls, tz=None): return cls._now
    monkeypatch.setattr(app, "datetime", _DT)

    _DT._now = datetime(2026, 11, 12, 12, 30, tzinfo=app.TZ_TAIPEI)     # 盤中 → 不補跑
    _, started, _ = _patch(monkeypatch, _FakeScanner("2026-11-10T08:48:40+00:00"))
    app.job_scan_boot_catchup()
    assert not started

    _DT._now = datetime(2026, 11, 12, 16, 41, tzinfo=app.TZ_TAIPEI)     # 盤後、今日未完成 → 補跑
    _, started, alerts = _patch(monkeypatch, _FakeScanner("2026-11-10T08:48:40+00:00"))
    app.job_scan_boot_catchup()
    assert len(started) == 1 and alerts

    _DT._now = datetime(2026, 11, 12, 16, 41, tzinfo=app.TZ_TAIPEI)     # 今日已完成 → 不補跑
    _, started, _ = _patch(monkeypatch, _FakeScanner("2026-11-12T08:48:40+00:00"))
    app.job_scan_boot_catchup()
    assert not started


def test_shadow_resolve_decoupled_from_scan(monkeypatch):
    import shadow
    calls = []
    monkeypatch.setattr(shadow, "resolve_pending", lambda: calls.append(1) or {"rows": 1})
    _patch(monkeypatch, _FakeScanner(scanning=True))
    app.job_shadow_resolve("t")
    assert not calls                                                     # 掃描中交由掃描收尾，不重疊
    _patch(monkeypatch, _FakeScanner(scanning=False))
    app.job_shadow_resolve("t")
    assert calls == [1]                                                  # 不依賴掃描也會結算


def test_jobs_registered_in_scheduler():
    src = open(app.__file__, encoding="utf-8").read()
    assert 'id="shadow_resolve_boot"' in src and 'id="scan_boot_catchup"' in src
    assert 'id="shadow_resolve_daily"' in src
