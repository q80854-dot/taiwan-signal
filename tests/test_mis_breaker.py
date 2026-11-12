"""MIS 斷路器（2026-11-12 系統檢查以線上日誌確認）。

線上證據：今日盤中 realtime.fetch_mis 每分鐘「第 1 次、第 2 次」皆 RemoteDisconnected
（第 2 次已是重建的新連線），價格全靠富果備援；每次失敗又會重建 session、多打一次 MIS 首頁，
失敗越多打越兇。昨日同時段只偶發 Read timed out 且第 2 次即成功——今日是證交所主動切斷。
"""
import realtime as rt


def _reset():
    rt._stats.update({"mis_consec_fail": 0, "mis_breaker_until": None, "mis_skipped": 0})
    rt._session = None


class _Sess:
    def __init__(self, fail=True, calls=None):
        self.fail = fail
        self.calls = calls if calls is not None else []
        self.headers = {}

    def get(self, url, **k):
        self.calls.append(url)
        if self.fail:
            raise ConnectionError("Remote end closed connection without response")

        class R:
            status_code = 200
            def json(self_): return {"msgArray": []}
        return R()


def test_breaker_trips_after_consecutive_failures_and_skips_requests(monkeypatch):
    _reset()
    calls = []
    monkeypatch.setattr(rt, "_get_session", lambda: _Sess(True, calls))
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    rt._fetch_batch(["tse_2330.tw"])
    rt._fetch_batch(["tse_2330.tw"])
    assert rt._mis_breaker_open()                        # 連續 2 批失敗 → 跳閘
    n = len(calls)
    assert rt._fetch_batch(["tse_2330.tw"]) == {}        # 斷路中直接回空
    assert len(calls) == n                               # 不再白打注定失敗的請求
    assert rt._stats["mis_skipped"] == 1


def test_breaker_resets_on_success(monkeypatch):
    _reset()
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    monkeypatch.setattr(rt, "_get_session", lambda: _Sess(True))
    rt._fetch_batch(["tse_2330.tw"])
    assert rt._stats["mis_consec_fail"] == 1
    monkeypatch.setattr(rt, "_get_session", lambda: _Sess(False))
    rt._fetch_batch(["tse_2330.tw"])
    assert rt._stats["mis_consec_fail"] == 0 and not rt._mis_breaker_open()


def test_breaker_reopens_probe_after_cooldown(monkeypatch):
    _reset()
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(rt, "_get_session", lambda: _Sess(True, calls))
    rt._fetch_batch(["a"]); rt._fetch_batch(["a"])
    rt._stats["mis_breaker_until"] = rt.time.time() - 1   # 冷卻結束
    before = len(calls)
    rt._fetch_batch(["a"])
    assert len(calls) > before                           # 冷卻後放一次試探
    assert rt._mis_breaker_open()                        # 試探仍失敗 → 立即再跳閘


def test_fresh_prices_falls_back_to_fugle_and_fails_closed(monkeypatch):
    """斷路中走既有富果備援；兩者皆無 → source_missing 不判斷，不以舊價替代。"""
    _reset()
    rt._stats["mis_breaker_until"] = rt.time.time() + 300
    today = rt.now_tpe().strftime("%Y-%m-%d")
    monkeypatch.setattr(rt, "fetch_fugle",
                        lambda c: {"price": 100.0, "date": today} if c == "2330" else None)
    out = rt.fresh_prices([("2330", "TSE"), ("2317", "TSE")])
    assert out["prices"] == {"2330": 100.0}
    assert out["reasons"] == {"2317": "source_missing"}
    _reset()


def test_fetch_indices_skips_mis_while_breaker_open(monkeypatch):
    """斷路中指數不再對 MIS 各試 2 次，直接走既有官方日線備援（標 stale，非盤中即時）。"""
    _reset()
    rt._stats["mis_breaker_until"] = rt.time.time() + 300
    calls = []
    monkeypatch.setattr(rt, "_get_session", lambda: _Sess(True, calls))
    try:
        rt.fetch_indices()
    except Exception:
        pass                                             # 備援來源在測試環境可能不可用，這裡只驗證不打 MIS
    assert calls == []
    _reset()
