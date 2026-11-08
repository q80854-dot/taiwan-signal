"""Market Regime baseline（規格 §6）：規則式波動/流動性分級與綜合標籤、缺資料標暫無。"""
import regime as R


def test_realized_vol_computes():
    # 固定 1% 日漲跌交替 → 年化波動約 1%*sqrt(252) ≈ 15.9%
    closes = [100.0]
    for i in range(25):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    v = R._realized_vol_annual(closes)
    assert v is not None and 10 < v < 25


def test_realized_vol_insufficient():
    assert R._realized_vol_annual([100, 101, 102]) is None


def test_vol_state_thresholds():
    assert R._vol_state(8)[0] == "low"
    assert R._vol_state(15)[0] == "normal"
    assert R._vol_state(30)[0] == "high"
    assert R._vol_state(None)[0] == "unknown"


def _stats(vals):
    return {f"2026-10-{i+1:02d}": {"value": v} for i, v in enumerate(vals)}


def test_liquidity_high_when_recent_surges():
    vals = [100] * 20 + [200] * 5           # 近 5 日放量
    st, zh, ratio = R._liquidity_state(_stats(vals))
    assert st == "high" and ratio >= 1.15


def test_liquidity_low_when_recent_shrinks():
    vals = [200] * 20 + [100] * 5
    st, zh, ratio = R._liquidity_state(_stats(vals))
    assert st == "low" and ratio <= 0.85


def test_liquidity_insufficient():
    assert R._liquidity_state({})[0] == "unknown"
    assert R._liquidity_state(_stats([1, 2, 3]))[0] == "unknown"


def test_compose_skips_unknown():
    assert R._compose("多頭", "高波動", "暫無") == "趨勢多頭、高波動"
    assert R._compose("暫無", "暫無", "暫無") == "市場狀態資料暫無"


def test_snapshot_structure(monkeypatch):
    # 全部資料源失敗 → 各維度標暫無、仍回 ok 結構
    import data_fetcher
    monkeypatch.setattr(data_fetcher, "fetch_market_regime", lambda: {"available": False})
    monkeypatch.setattr(data_fetcher, "_twse_index_daily_closes", lambda months=3: [])
    monkeypatch.setattr(data_fetcher, "twse_market_stats", lambda months=2: {})
    snap = R.regime_snapshot()
    assert snap["ok"] is True
    for k in ("trend", "volatility", "liquidity", "breadth"):
        assert snap["components"][k]["state"] == "unknown"
    assert snap["composite"] == "市場狀態資料暫無"
