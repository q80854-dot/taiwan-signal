"""關鍵失敗情境 fail-closed 契約（治理 TS-TEST-001，2026-11-08）。

把「關鍵資料缺失/極端風險時不得放行新單」這組風控不變量鎖成測試。涵蓋：
- 大盤資料缺失 → 不可交易（env_score=None）
- 每日虧損風控狀態未知（DB 讀取失敗）→ 不可交易（F-07/F-08）
- 外資賣超暫停等級（extreme/stop_buy）→ 不可交易
- 外資賣超三層（warning/heavy/extreme）在 run_all_checks 的扣分一致（2026-11-08 重校）
全部離線、確定性。
"""
import risk_manager as rm


_OK_INDEX = {"index": {"twii": {"chg": 0.1, "price": 20000, "source": "ok"},
                       "vix": {"price": 18, "source": "ok"}}}


def _mo(**extra):
    mo = {"index": {"twii": {"chg": 0.1, "price": 20000, "source": "ok"},
                    "vix": {"price": 18, "source": "ok"}},
          "margin": {"chg_pct": 0}}  # 提供 margin 資料，避免 margin_chg unknown 的 −3 疊加污染
    mo.update(extra)
    return mo


def test_no_trade_when_index_missing(monkeypatch):
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    st = rm.get_system_status({"index": {}, "foreign": {}})
    assert st["env_score"] is None and st["can_trade"] is False


def test_no_trade_when_daily_risk_unavailable(monkeypatch):
    """F-07：每日虧損風控讀取失敗（unavailable）→ 儀表板不可顯示可交易。"""
    monkeypatch.setattr(rm, "check_daily_loss_limit",
                        lambda: {"unavailable": True, "message": "🔴 風控狀態讀取失敗"})
    st = rm.get_system_status(_mo(foreign={"net_buy_twd": 0}))
    assert st["can_trade"] is False


def test_run_all_checks_blocks_when_daily_risk_unavailable(monkeypatch):
    monkeypatch.setattr(rm, "check_daily_loss_limit",
                        lambda: {"unavailable": True, "message": "🔴 風控狀態讀取失敗"})
    res = rm.run_all_checks("2330.TW", {"close": 600},
                            {"daily": {"closes": [100.0, 100.2, 100.1]}}, _mo(foreign={"net_buy_twd": 0}))
    assert res["can_signal"] is False and any("風控狀態" in b for b in res["blockers"])


def test_foreign_extreme_sell_halts_trading():
    """外資賣超 extreme（≥1,800億，stop_buy）→ 儀表板不可交易。"""
    st = rm.get_system_status(_mo(foreign={"net_buy_twd": -2000e8}))
    assert st["stop_buy"] is True and st["can_trade"] is False


def test_market_circuit_breaker_marks_missing_data():
    """大盤/VIX 缺資料 → data_available=False（不可被當成乾淨滿分）。"""
    r = rm.check_market_circuit_breaker({"index": {}})
    assert r.get("data_available") is False


def test_foreign_three_tier_score_adj(monkeypatch):
    """run_all_checks 外資三層扣分一致：warning −8、heavy −12、extreme −12。"""
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    tf = {"daily": {"closes": [100.0, 100.2, 100.1]}}  # 不觸發 spike

    def adj(nb):
        res = rm.run_all_checks("2330.TW", {"close": 600}, tf, _mo(foreign={"net_buy_twd": nb}))
        return res["score_adj"]

    assert adj(-600e8) == -8     # warning
    assert adj(-1200e8) == -12   # heavy
    assert adj(-2000e8) == -12   # extreme
    assert adj(-100e8) == 0      # 日常賣超、不扣分
