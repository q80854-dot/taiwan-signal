"""2026-11-14 稽核修正（使用者同意；不改策略）的回歸測試。"""
from datetime import datetime

import pytest

import scanner as scanner_mod
from scanner import TWScanEngine


# ── BUG-1／BUG-5：正式結算遇除權息（Yahoo 還原價）＋ ETF 證交稅 ──
def _run_resolve(monkeypatch, ticker, bars_adj):
    from state_store import store
    import data_fetcher, risk_manager
    sig = {"id": "t1", "ticker": ticker, "code": ticker.split(".")[0], "name": "測試", "direction": "buy",
           "stop_loss": 95.0, "tp1": 110.0, "tp2": 120.0, "tp3": 130.0, "entry_price": 100.0, "current_price": 100.0,
           "suggested_shares": 3000, "generated_at": "2026-10-01T08:30:00+00:00", "raw_json": "{}"}
    got = []
    monkeypatch.setattr(store, "get_pending_signals", lambda *a, **k: [sig])
    monkeypatch.setattr(store, "update_signal_result", lambda *a, **k: got.append(a))
    monkeypatch.setattr(store, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(risk_manager, "record_signal_loss", lambda *a, **k: None)
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf: bars_adj)
    for name in ("send_alert", "send_message", "push_signal_result", "send_signal_result"):
        if hasattr(scanner_mod, name):
            monkeypatch.setattr(scanner_mod, name, lambda *a, **k: None, raising=False)
    eng = TWScanEngine()
    monkeypatch.setattr(eng, "_call_with_timeout", lambda sec, fn, *a: fn(*a))
    eng._resolve_pending_signals()
    return got


def _adj_series(f):
    """訊號日 10/01 收盤 100；10/05 除息 4%。Yahoo 還原價把除息日之前全部乘 f=0.96。
    未還原：10/02 開 100、低 96（未觸停損 95）；10/07 衝到 135（三段停利都到）。"""
    raw = [("2026-10-01", 100, 101, 99, 100), ("2026-10-02", 100, 101, 96, 98), ("2026-10-05", 94, 97, 93.5, 96),
           ("2026-10-06", 97, 110, 96.5, 108), ("2026-10-07", 108, 135, 107, 134)]
    out = {"dates": [], "opens": [], "highs": [], "lows": [], "closes": []}
    for d, o, h, l, c in raw:
        k = f if d < "2026-10-05" else 1.0
        out["dates"].append(d); out["opens"].append(o * k); out["highs"].append(h * k)
        out["lows"].append(l * k); out["closes"].append(c * k)
    return out


def test_settlement_rescales_adjusted_bars_no_fake_stop(monkeypatch):
    got = _run_resolve(monkeypatch, "2330.TW", _adj_series(0.96))
    assert got, "應該要結算"
    _id, result, close_price, pnl, pnl_pct = got[0][:5]
    # 還原價 10/02 低點 96×0.96=92.16 會被誤判跌破停損 95；換回訊號當時單位後不應停損
    assert pnl > 0 and "stop" not in str(result).lower(), got[0]


def _grossed(f=0.96):
    """同一段行情、但已是「訊號當時價格單位」（訊號日收盤＝記錄值，不需縮放）。"""
    a = _adj_series(f)
    return {k: ([x / f for x in v] if k != "dates" else v) for k, v in a.items()}


def test_settlement_rescaled_equals_unscaled_reference(monkeypatch):
    fixed = _run_resolve(monkeypatch, "2330.TW", _adj_series(0.96))[0]
    ref = _run_resolve(monkeypatch, "2330.TW", _grossed(0.96))[0]       # 不觸發縮放的對照
    assert fixed[1] == ref[1] and abs(fixed[3] - ref[3]) < 1e-6


def test_settlement_etf_uses_etf_tax(monkeypatch):
    stock = _run_resolve(monkeypatch, "2330.TW", _grossed())[0][3]
    etf = _run_resolve(monkeypatch, "0050.TW", _grossed())[0][3]
    assert etf > stock                       # ETF 證交稅 0.1% < 股票 0.3%，同樣價位損益較高


# ── BUG-2：同日第二次掃描只略過總結報告，新訊號照推；日期用台北時間 ──
def test_second_scan_same_day_still_pushes_new_signals(monkeypatch):
    from state_store import store
    tpe_today = datetime.now(scanner_mod.timezone(scanner_mod.timedelta(hours=8))).strftime("%Y-%m-%d")
    monkeypatch.setattr(store, "get_meta", lambda k, d=None: tpe_today if k == "daily_report_sent_date" else d)
    reports, pushed = [], []
    import telegram_bot as tb
    monkeypatch.setattr(tb, "send_daily_report", lambda *a, **k: reports.append(1))
    monkeypatch.setattr(tb, "push_signal", lambda s: pushed.append(s["ticker"]) or {"any_ok": True})
    monkeypatch.setattr(tb, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(scanner_mod.time, "sleep", lambda s: None)
    import shadow
    monkeypatch.setattr(shadow, "mark_delivered", lambda d: None)
    eng = TWScanEngine()
    monkeypatch.setattr(eng, "_apply_notify_prefs", lambda sigs: sigs)
    eng._push_signals([{"ticker": "2330.TW", "code": "2330", "name": "台積電"}], {}, {})
    assert reports == [] and pushed == ["2330.TW"]


# ── BUG-3：故障告警跳脫 HTML，送不出時改純文字 ──
def test_send_alert_escapes_html_and_falls_back(monkeypatch):
    import telegram_bot as tb
    sent = []
    monkeypatch.setattr(tb, "_load_subscribers", lambda: {"admin": ["1"]})
    monkeypatch.setattr(tb, "send_message", lambda cid, text, parse_mode="HTML", **k: sent.append((text, parse_mode)) or False)
    try:
        from state_store import store
        monkeypatch.setattr(store, "add_event", lambda *a, **k: None)
    except Exception:
        pass
    tb.send_alert("TypeError: '<' not supported between <urllib3.connection>", "error")
    assert "&lt;" in sent[0][0] and "<urllib3" not in sent[0][0] and sent[0][1] == "HTML"
    assert sent[1][1] == "" and "<urllib3" in sent[1][0]          # 第二次為純文字原文


# ── BUG-4：休市表「開始交易日／最後交易日」不是休市；官方只回一年時以靜態清單補 ──
def test_holiday_schedule_excludes_trading_day_rows(monkeypatch):
    import data_fetcher as df
    rows = [{"Name": "中華民國開國紀念日", "Date": "1160101"},
            {"Name": "國曆新年開始交易日", "Date": "1160104"},
            {"Name": "農曆春節前最後交易日", "Date": "1160201"},
            {"Name": "市場無交易，僅辦理結算交割作業。", "Date": "1160202"},
            {"Name": "農曆春節後開始交易日", "Date": "1160211"}]

    class R:
        status_code = 200
        def json(self): return rows
    monkeypatch.setattr(df.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(df, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(df, "_cache_set", lambda k, v: v)
    monkeypatch.setattr(df, "_holiday_fail_until", 0)
    got = df._fetch_official_holiday_set()
    assert got == {"2027-01-01", "2027-02-02"}
    monkeypatch.setattr(df, "_fetch_official_holiday_set", lambda: got)
    assert df._is_national_holiday(datetime(2027, 1, 4)) is False       # 開始交易日照常開盤
    assert df._is_national_holiday(datetime(2026, 12, 25)) is True      # 官方只回 2027，2026 年由靜態清單補
    assert df._is_national_holiday(datetime(2027, 2, 2)) is True


# ── BUG-7：盤中手動掃描遇到今天尚未收盤的日 K → 略過 ──
def test_unfinal_intraday_bar_is_skipped(monkeypatch):
    import threading, data_fetcher
    eng = TWScanEngine()
    eng._exp_bar_date = "2026-11-13"
    eng._pre = {"skipped": 0, "passed": 0, "audited": 0, "mismatch": 0, "stale_skipped": 0, "stale_samples": []}
    eng._pre_lock = threading.Lock()
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv",
                        lambda t, tf: {"dates": ["2026-11-13", "2026-11-16"], "closes": [1, 2], "opens": [1, 2],
                                       "highs": [1, 2], "lows": [1, 2]})
    called = []
    monkeypatch.setattr(eng, "_reconcile_last_bar", lambda *a: called.append(1) or a[2])
    out = eng._scan_single("2330.TW", {}, None, None, None, lambda t: {"code": "2330"})
    assert out is None and not called and eng._pre["unfinal_skipped"] == 1
