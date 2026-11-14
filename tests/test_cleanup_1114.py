"""2026-11-14 第一批清理（使用者同意；不改變任何正式訊號）的回歸測試。"""
import time
import uuid
from datetime import datetime

import pytest

import members
import app as app_module

H = {"X-TS": "1"}


@pytest.fixture()
def cl(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    monkeypatch.setenv("OWNER_EMAILS", "owner@example.com")
    members._ready["ok"] = False
    app_module.app.config["TESTING"] = True
    app_module._RATE.clear()
    return app_module.app.test_client()


def _login(c, email):
    sub = "t-" + uuid.uuid4().hex[:10]
    members.upsert_member(sub, email, "測試者")
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


def test_foreign_flow_research_owner_only(cl, monkeypatch):
    import analysis_foreign
    called = []
    monkeypatch.setattr(analysis_foreign, "start", lambda *a, **k: called.append(1), raising=False)
    _login(cl, "member@example.com")
    assert cl.get("/api/analysis/foreign-flow").status_code == 403     # 一般會員不能觸發 5～8 分鐘背景運算
    assert not called


def test_research_full_backtests_offline_by_default(cl, monkeypatch):
    monkeypatch.delenv("RESEARCH_BACKTEST_ONLINE", raising=False)
    started = []
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: started.append(1)})())
    _login(cl, "owner@example.com")
    for p in ("compare", "short_gate_validation", "short_gate_validation_bear", "partial_exit", "ablation"):
        r = cl.post(f"/api/backtest/{p}/run", headers=H)
        assert r.status_code == 410 and r.get_json()["status"] == "disabled", p
        assert cl.get(f"/api/backtest/{p}/result").status_code == 200, p   # 已存結果仍可讀
    assert not started


def test_research_full_backtest_can_be_reenabled(cl, monkeypatch):
    monkeypatch.setenv("RESEARCH_BACKTEST_ONLINE", "1")
    started = []
    monkeypatch.setattr(app_module.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: started.append(1)})())
    app_module._ablation_bt_running.clear()
    _login(cl, "owner@example.com")
    r = cl.post("/api/backtest/ablation/run", headers=H)
    assert r.status_code == 200 and r.get_json()["status"] == "started" and started


@pytest.mark.parametrize("when,skipped_second", [
    (datetime(2026, 11, 14, 12, 0), True),    # 週六 → 非交易時段，第二次在間隔內略過
    (datetime(2026, 11, 16, 23, 0), True),    # 週一深夜
    (datetime(2026, 11, 17, 6, 0), True),     # 週二清晨
    (datetime(2026, 11, 16, 10, 0), False),   # 平日盤中照原頻率
    (datetime(2026, 11, 16, 16, 30), False),  # 盤後掃描時段照原頻率
])
def test_offhours_skip(monkeypatch, when, skipped_second):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return app_module.TZ_TAIPEI.localize(when) if hasattr(app_module.TZ_TAIPEI, "localize") else when.replace(tzinfo=tz)
    monkeypatch.setattr(app_module, "datetime", _DT)
    app_module._OFFHOURS_LAST.clear()
    assert app_module._offhours_skip("k", 1800) is False          # 第一次一定跑
    assert app_module._offhours_skip("k", 1800) is skipped_second


def test_finmind_shares_quota_accounting(monkeypatch):
    import finmind
    import finmind_data
    hits = []
    monkeypatch.setattr(finmind, "circuit_open", lambda: False)
    monkeypatch.setattr(finmind, "_token", lambda: "")
    monkeypatch.setattr(finmind.requests, "get", lambda *a, **k: hits.append(1) or (_ for _ in ()).throw(RuntimeError("no net")))
    monkeypatch.setattr(finmind_data, "budget_left", lambda: 0)
    assert finmind._fetch("TaiwanStockNews", "2330", "2026-11-01") == []
    assert not hits                                                 # 額度到保護線：不再對外連線
    monkeypatch.setattr(finmind_data, "budget_left", lambda: 100)
    monkeypatch.setattr(finmind_data, "_blocked_until", 0.0)
    n0 = len(finmind_data._calls)
    finmind._fetch("TaiwanStockNews", "2330", "2026-11-01")
    assert hits and len(finmind_data._calls) == n0 + 1              # 有連線就記進共用額度


def test_moved_helpers_still_available():
    from backtester import calc_performance_metrics
    m = calc_performance_metrics([100, 110, 105, 120], [{"pnl": 10}, {"pnl": -5}, {"pnl": 15}])
    assert m["valid"] and m["n_trades"] == 3 and m["total_return"] == 20.0
    assert calc_performance_metrics([100, 0])["valid"] is False
    from realtime import fugle_key_diagnostic
    import os
    if not os.environ.get("FUGLE_API_KEY"):
        assert fugle_key_diagnostic() == {"ok": False, "detail": "未設定 FUGLE_API_KEY"}
