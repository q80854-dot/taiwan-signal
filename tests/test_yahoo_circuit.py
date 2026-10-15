"""Yahoo 熔斷器（2026-10-15）：連續 401／逾時後跳閘，冷卻期間直接回 None（呼叫端退回快取），
成功後清零。避免 Yahoo 偶發故障時 yfinance 反覆重試把個股研究頁拖到逾時。"""
import data_fetcher as df


class _FakeTicker:
    def __init__(self, exc=None, empty=False):
        self._exc = exc; self._empty = empty
    def history(self, **k):
        if self._exc:
            raise self._exc
        import types
        return types.SimpleNamespace(empty=self._empty)


def _reset():
    df._YF_CB.update({"fails": 0, "until": 0.0})


def test_circuit_trips_after_threshold(monkeypatch):
    _reset()
    monkeypatch.setattr(df, "YFINANCE_OK", True)
    monkeypatch.setattr(df, "yf", type("Y", (), {"Ticker": staticmethod(lambda s: _FakeTicker(exc=Exception("HTTP Error 401: Invalid Crumb")))}), raising=False)
    # 門檻前：每次都丟出例外（呼叫端自行 try）
    for _ in range(df._YF_FAIL_THRESHOLD):
        try:
            df._yf_history("2330.TW", period="5y")
        except Exception:
            pass
    assert df.yahoo_circuit_open() is True
    # 跳閘後：直接回 None，不再呼叫 yfinance（就算會丟例外也不會被呼叫到）
    assert df._yf_history("2330.TW", period="5y") is None


def test_circuit_resets_on_success(monkeypatch):
    _reset()
    monkeypatch.setattr(df, "YFINANCE_OK", True)
    df._YF_CB["fails"] = 2
    monkeypatch.setattr(df, "yf", type("Y", (), {"Ticker": staticmethod(lambda s: _FakeTicker(empty=False))}), raising=False)
    h = df._yf_history("2330.TW", period="1y")
    assert h is not None and df._YF_CB["fails"] == 0


def test_non_down_error_does_not_trip(monkeypatch):
    _reset()
    monkeypatch.setattr(df, "YFINANCE_OK", True)
    monkeypatch.setattr(df, "yf", type("Y", (), {"Ticker": staticmethod(lambda s: _FakeTicker(exc=ValueError("weird parse")))}), raising=False)
    for _ in range(5):
        try:
            df._yf_history("2330.TW", period="5y")
        except Exception:
            pass
    assert df.yahoo_circuit_open() is False     # 非連線類錯誤不跳閘
