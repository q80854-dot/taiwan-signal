"""資料品質列舉與分類（治理 Phase 2 / PIT 起步 A，2026-11-08）。

驗證 data_quality 純函式，以及 get_system_status 新增的 data_quality 欄位（純顯示、不改決策）。
"""
import data_quality as dq
import risk_manager as rm


def test_worst_picks_most_severe():
    assert dq.worst(dq.VALID, dq.PARTIAL, dq.INVALID) == dq.INVALID
    assert dq.worst(dq.VALID, dq.DELAYED) == dq.DELAYED
    assert dq.worst() == dq.VALID           # 空輸入視為 VALID
    assert dq.worst(None, None) == dq.VALID


def test_is_tradeable():
    assert dq.is_tradeable(dq.VALID) and dq.is_tradeable(dq.DELAYED) and dq.is_tradeable(dq.PARTIAL)
    assert not dq.is_tradeable(dq.STALE)
    assert not dq.is_tradeable(dq.MISSING)
    assert not dq.is_tradeable(dq.INVALID)


def test_classify_freshness():
    assert dq.classify_freshness(None, now=100.0, max_age_seconds=60) == dq.MISSING
    assert dq.classify_freshness(100.0, now=100.0, max_age_seconds=None) == dq.VALID
    assert dq.classify_freshness(100.0, now=130.0, max_age_seconds=60) == dq.VALID   # 30s < 60s
    assert dq.classify_freshness(100.0, now=200.0, max_age_seconds=60) == dq.STALE   # 100s > 60s
    assert dq.classify_freshness(200.0, now=100.0, max_age_seconds=60) == dq.INVALID # 時間戳在未來


_OK = {"index": {"twii": {"chg": 0.1, "price": 20000, "source": "ok"},
                 "vix": {"price": 18, "source": "ok"}},
       "foreign": {"net_buy_twd": 0}, "margin": {"chg_pct": 0}}


def test_system_status_quality_valid(monkeypatch):
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    st = rm.get_system_status(_OK)
    assert st["data_quality"] == dq.VALID
    assert st["can_trade"] is True       # 行為不變


def test_system_status_quality_invalid_when_index_missing(monkeypatch):
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    st = rm.get_system_status({"index": {}, "foreign": {}})
    assert st["data_quality"] == dq.INVALID
    assert st["can_trade"] is False      # fail-closed 不變


def test_system_status_quality_partial_when_only_vix_missing(monkeypatch):
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    st = rm.get_system_status({"index": {"twii": {"chg": 0.1, "price": 20000, "source": "ok"}},
                               "foreign": {"net_buy_twd": 0}, "margin": {"chg_pct": 0}})
    assert st["data_quality"] == dq.PARTIAL
