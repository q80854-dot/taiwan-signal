"""大盤中期趨勢 regime（2026-10-15）：由指數日收盤序列計算，不依賴 Yahoo。"""
import data_fetcher as df


def test_regime_bullish():
    closes = [100 + i * 0.5 for i in range(70)]       # 穩定走高
    r = df._regime_from_closes(closes, "t")
    assert r["trend"] == "bullish" and r["bearish_regime"] is False and r["available"] is True


def test_regime_bearish():
    closes = [200 - i * 0.5 for i in range(70)]       # 穩定走低
    r = df._regime_from_closes(closes, "t")
    assert r["trend"] == "bearish" and r["bearish_regime"] is True


def test_regime_insufficient():
    assert df._regime_from_closes([1, 2, 3], "t") is None
    assert df._regime_from_closes([], "t") is None


def test_regime_prefers_official(monkeypatch):
    # 官方序列可用時，直接用官方、完全不碰 yfinance
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(df, "_cache_set", lambda k, v: v)
    monkeypatch.setattr(df, "_twse_index_daily_closes", lambda months=5: [100 + i * 0.5 for i in range(70)])
    def _boom(*a, **k):
        raise AssertionError("不應呼叫 yfinance")
    monkeypatch.setattr(df, "_yf_history", _boom)
    r = df.fetch_market_regime()
    assert r["available"] and r["source"] == "twse_official"
