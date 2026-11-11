"""LIVE03（第三階段審查）：風控三上限單一事實來源、語意不混用。

審查 §9.5 LIVE03：各頁把『單日新增 6%』當『組合上限』顯示、歷史訊號 1.2% 與設定頁 2% 混淆。
risk_basis() 把三個語意不同的上限各自命名回傳，所有頁面一律引用、不各自硬編。
"""
import risk_manager as rm
import config as C


def test_risk_basis_three_caps_distinct_and_named():
    rb = rm.risk_basis()
    assert rb["per_trade_pct"] == round(C.MAX_RISK_PER_TRADE * 100, 2)
    assert rb["daily_new_pct"] == round(C.MAX_DAILY_RISK * 100, 2)
    assert rb["portfolio_hard_cap_pct"] == round(C.MAX_PORTFOLIO_RISK * 100, 2)
    assert rb["max_positions"] == C.MAX_SIMULTANEOUS_POSITIONS
    # 核心 bug：組合硬上限 ≠ 單日新增（不可混用）
    assert rb["portfolio_hard_cap_pct"] != rb["daily_new_pct"]
    assert rb.get("config_version") and rb.get("effective_at")
    assert "不可混用" in rb.get("basis", "")


def test_risk_basis_balance_override():
    rb = rm.risk_basis(balance=1_000_000)
    assert rb["account_balance_twd"] == 1_000_000


def test_portfolio_risk_exposes_portfolio_hard_cap():
    # 前端組合風險面板讀 max_portfolio_risk_pct，必須＝組合硬上限（非單日）
    pr = rm.portfolio_risk([
        {"code": "2330", "name": "台積電", "direction": "buy", "sector": "半導體",
         "suggested_shares": 1000, "entry_price": 100.0, "stop_loss": 95.0},
    ], balance=500_000)
    assert pr["max_portfolio_risk_pct"] == round(C.MAX_PORTFOLIO_RISK * 100, 1)
    assert pr["max_daily_risk_pct"] == round(C.MAX_DAILY_RISK * 100, 1)
