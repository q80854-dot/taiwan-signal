"""LIVE01（第三階段審查）：持倉現價已到/穿越停損 → breached（已觸停待確認），不與「近停損」混淆。

_position_view 應據多/空方向正確標記 breached，並給穿越幅度 breach_pct；未穿越者 breached=False。
"""
import app as app_module


def _patch_bars(monkeypatch, last):
    data = {"current_price": last, "dates": ["2026-10-02", "2026-10-03"],
            "closes": [last, last], "highs": [last + 1, last + 1], "lows": [last - 1, last - 1]}
    monkeypatch.setattr(app_module, "_bars_since", lambda ticker, gen: (data, [0, 1]))


def _row(direction, entry, sl):
    return {"id": 1, "ticker": "X.TW", "code": "0001", "name": "測試", "direction": direction,
            "entry_price": entry, "stop_loss": sl, "generated_at": "2026-10-01", "result": "pending",
            "status": "active"}


def test_short_breached_when_price_at_or_above_stop(monkeypatch):
    _patch_bars(monkeypatch, last=729)                       # 空單現價 729 ≥ 停損 721.44 → 已穿越
    out = app_module._position_view(_row("sell", 668, 721.44))
    assert out["breached"] is True
    assert out["breach_pct"] == round((729 - 721.44) / 721.44 * 100, 2)


def test_short_not_breached_when_price_below_stop(monkeypatch):
    _patch_bars(monkeypatch, last=700)                       # 空單現價 700 < 停損 721.44 → 未穿越
    out = app_module._position_view(_row("sell", 668, 721.44))
    assert out["breached"] is False
    assert "breach_pct" not in out


def test_long_breached_when_price_at_or_below_stop(monkeypatch):
    _patch_bars(monkeypatch, last=94)                        # 多單現價 94 ≤ 停損 95 → 已穿越
    out = app_module._position_view(_row("buy", 100, 95))
    assert out["breached"] is True
    assert out["breach_pct"] == round((95 - 94) / 95 * 100, 2)


def test_long_not_breached_when_price_above_stop(monkeypatch):
    _patch_bars(monkeypatch, last=98)
    out = app_module._position_view(_row("buy", 100, 95))
    assert out["breached"] is False
