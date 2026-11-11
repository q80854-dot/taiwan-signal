"""策略引擎 2b-2：scanner 掃描 hook 的接線與隔離。

驗證 _scan_single 在通過預篩後會呼叫 strategies.runner.run_and_record（平行記錄），且：
① 策略錯誤絕不中斷掃描（仍回傳 gen_signal 結果）；② STRATEGY_SHADOW=0 可即時關閉。
純 shadow、不影響真實訊號產生。
"""
import scanner as scn
import data_fetcher
import signal_engine
from strategies import runner


_DAILY = {"dates": ["2026-10-02", "2026-10-03"], "closes": [100, 101],
          "highs": [101, 102], "lows": [99, 100], "opens": [100, 101]}
_SENTINEL = {"_sig": "ok"}


def _setup(monkeypatch, run_impl):
    tw = scn.TWScanEngine()
    tw._pre = {"skipped": 0, "passed": 0, "audited": 0, "mismatch": 0}
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": _DAILY)
    monkeypatch.setattr(signal_engine, "check_multi_timeframe_tw",
                        lambda tf, **k: {"direction": "buy", "score": 99})  # 通過預篩
    monkeypatch.setattr(tw, "_reconcile_last_bar", lambda ticker, info, d: d)
    monkeypatch.setattr(runner, "run_and_record", run_impl)
    get_info = lambda t: {"code": "2330", "name": "x", "size_cat": "中型股"}
    fetch_tf = lambda t: {"daily": _DAILY}
    fetch_inst = lambda c: {}
    gen = lambda t, info, tf, mkt, inst: _SENTINEL
    return tw, get_info, fetch_tf, fetch_inst, gen


def test_hook_calls_run_and_record(monkeypatch):
    monkeypatch.delenv("STRATEGY_SHADOW", raising=False)
    calls = []
    tw, get_info, fetch_tf, fetch_inst, gen = _setup(
        monkeypatch, lambda bd, tk, si, tf, mkt, inst=None: calls.append((bd, tk)) or 1)
    sig = tw._scan_single("2330.TW", {}, fetch_tf, fetch_inst, gen, get_info)
    assert sig is _SENTINEL                       # 掃描照常回傳正式訊號
    assert calls == [("2026-10-03", "2330.TW")]   # 以最新 bar_date 平行記錄


def test_hook_error_never_breaks_scan(monkeypatch):
    monkeypatch.delenv("STRATEGY_SHADOW", raising=False)
    def _boom(*a, **k):
        raise RuntimeError("strategy exploded")
    tw, get_info, fetch_tf, fetch_inst, gen = _setup(monkeypatch, _boom)
    sig = tw._scan_single("2330.TW", {}, fetch_tf, fetch_inst, gen, get_info)
    assert sig is _SENTINEL                        # 策略例外被吞、掃描不中斷


def test_hook_killswitch_disables(monkeypatch):
    monkeypatch.setenv("STRATEGY_SHADOW", "0")
    called = []
    tw, get_info, fetch_tf, fetch_inst, gen = _setup(
        monkeypatch, lambda *a, **k: called.append(1) or 1)
    sig = tw._scan_single("2330.TW", {}, fetch_tf, fetch_inst, gen, get_info)
    assert sig is _SENTINEL
    assert called == []                            # 關閉時不呼叫
