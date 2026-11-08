"""單位治本：DB 訊號列一律帶明確的 suggested_shares（治理 TS-DATA-001，2026-11-08）。

根因：DB `signals.suggested_lots` 欄位名叫「張」但歷史遺留實存「股」，同名依來源有兩種語意，
讀取端易 1000× 誤讀（見 docs/UNIT_AUDIT_2026-11-06.md）。治本：資料存取邊界一律補上語意
明確的 suggested_shares（股），且不更動既有 suggested_lots 值（今日數值零變動、零回歸）。
"""
import state_store
from state_store import store, with_share_units


def test_with_share_units_fills_shares_from_legacy_lots():
    """DB 列：suggested_lots 實存股數 → 補出相同數值的 suggested_shares，且不更動 lots。"""
    row = {"suggested_lots": 2500}
    out = with_share_units(row)
    assert out["suggested_shares"] == 2500       # 明確股數
    assert out["suggested_lots"] == 2500         # 既有值不動（相容風控/舊讀取端）


def test_with_share_units_does_not_clobber_existing_shares():
    """已帶 suggested_shares（如記憶體訊號 dict，lots＝張）者不得被覆蓋。"""
    row = {"suggested_shares": 3000, "suggested_lots": 3}
    out = with_share_units(row)
    assert out["suggested_shares"] == 3000
    assert out["suggested_lots"] == 3


def test_with_share_units_handles_missing_lots():
    """沒有 suggested_lots 時不應炸、也不硬塞。"""
    out = with_share_units({"ticker": "2330.TW"})
    assert "suggested_shares" not in out


def _save(sid, shares, status="active", result="pending"):
    assert store.save_signal({
        "id": sid, "ticker": "2330.TW", "name": "台積電", "direction": "buy",
        "score": 7.0, "current_price": 1000.0, "entry_price": 1000.0,
        "stop_loss": 950.0, "tp1": 1080.0,
        "suggested_shares": shares, "suggested_lots": round(shares / 1000, 3),
        "risk_twd": 5000, "status": status, "result": result,
    }) is True


def test_db_readers_expose_explicit_shares():
    """get_recent_signals / get_pending_signals / get_closed_trades 回傳列都帶 suggested_shares（股），
    數值等於落盤的股數（DB suggested_lots 欄位存的就是股數）。"""
    _save("units-active-1", 2500, status="active", result="pending")
    _save("units-closed-1", 1800, status="closed", result="tp1")

    recent = {r["id"]: r for r in store.get_recent_signals(limit=50, days_back=3650)}
    assert recent["units-active-1"]["suggested_shares"] == 2500
    assert recent["units-closed-1"]["suggested_shares"] == 1800

    pend = {r["id"]: r for r in store.get_pending_signals()}
    assert pend["units-active-1"]["suggested_shares"] == 2500

    closed = {r["id"]: r for r in store.get_closed_trades(limit=50)}
    assert closed["units-closed-1"]["suggested_shares"] == 1800
