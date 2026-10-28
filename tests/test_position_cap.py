"""部位資金上限：同時持倉全滿時，進場成本合計不得超過帳戶資金（2026-10-28 使用者回報合計超過 50 萬）。"""
from config import ACCOUNT_BALANCE_TWD, MAX_SIMULTANEOUS_POSITIONS
from signal_engine import calc_position_size, fit_to_cash


def test_single_position_capped_at_balance_over_max_positions():
    # 停損很近 → 風險預算換算的股數很大，應被「資金 ÷ 持倉上限」壓住
    pos = calc_position_size(50.0, 49.5)
    assert pos["position_value"] <= ACCOUNT_BALANCE_TWD / MAX_SIMULTANEOUS_POSITIONS + 50


def test_full_book_never_exceeds_balance():
    total = sum(calc_position_size(p, p * 0.97)["position_value"] for p in (49.3, 48.2, 135.5, 50.6, 778.0))
    assert total <= ACCOUNT_BALANCE_TWD


def test_fit_to_cash_downsizes_and_recomputes():
    sig = {"entry_price": 100.0, "stop_loss": 95.0, "suggested_shares": 2000, "suggested_lots": 2.0,
           "risk_twd": 10000, "risk_pct": 2.0, "position_value": 200000}
    out = fit_to_cash(sig, 50000)
    assert out["suggested_shares"] == 500
    assert out["position_value"] == 50000 and out["risk_twd"] == 2500
    assert out["suggested_lots"] == 0.5


def test_fit_to_cash_keeps_signal_when_cash_enough_and_drops_when_none():
    sig = {"entry_price": 100.0, "stop_loss": 95.0, "suggested_shares": 100}
    assert fit_to_cash(dict(sig), 1e6)["suggested_shares"] == 100
    assert fit_to_cash(dict(sig), 50) is None


def test_fit_to_cash_respects_portfolio_risk_budget():
    sig = {"entry_price": 100.0, "stop_loss": 95.0, "suggested_shares": 1000}
    out = fit_to_cash(sig, 1e6, risk_left=2000)   # 每股風險 5 → 最多 400 股
    assert out["suggested_shares"] == 400 and out["risk_twd"] == 2000
    assert fit_to_cash({"entry_price": 100.0, "stop_loss": 95.0, "suggested_shares": 10}, 1e6, risk_left=0) is None


def test_plan_rescale_with_live_positions_fits_balance_and_risk():
    # 2026-10-28 正式站 5 檔追蹤部位（舊規則單檔 30%，合計 744,654 > 500,000）
    from risk_manager import plan_rescale
    rows = [
        {"id": 1, "code": "6112", "entry_price": 49.3, "stop_loss": 46.69, "suggested_lots": 3042, "generated_at": "2026-10-26T08:00"},
        {"id": 2, "code": "2031", "entry_price": 48.2, "stop_loss": 46.62, "suggested_lots": 3112, "generated_at": "2026-10-26T08:01"},
        {"id": 3, "code": "3010", "entry_price": 135.5, "stop_loss": 130.24, "suggested_lots": 1107, "generated_at": "2026-10-26T08:02"},
        {"id": 4, "code": "2027", "entry_price": 50.6, "stop_loss": 48.73, "suggested_lots": 2964, "generated_at": "2026-10-26T08:03"},
        {"id": 5, "code": "2379", "entry_price": 778.0, "stop_loss": 724.36, "suggested_lots": 186, "generated_at": "2026-10-21T08:00"},
    ]
    plan = {p["id"]: p for p in plan_rescale(rows, balance=500000, max_positions=5, max_risk=0.06)}
    final = {r["id"]: plan[r["id"]]["after"] if r["id"] in plan else r["suggested_lots"] for r in rows}
    cost = sum(final[r["id"]] * r["entry_price"] for r in rows)
    risk = sum(final[r["id"]] * abs(r["entry_price"] - r["stop_loss"]) for r in rows)
    assert cost <= 500000 and risk <= 30000
    assert all(final[r["id"]] * r["entry_price"] <= 100000 for r in rows)
    assert all(final[r["id"]] <= r["suggested_lots"] for r in rows)      # 只縮不放
    assert all(v >= 1 for v in final.values())
