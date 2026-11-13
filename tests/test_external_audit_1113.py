"""2026-11-13 外部審查（Institutional-Grade 深度審查）第一批：安全性與一致性。不改進出場參數。

P0-1 發訊前的資金／風險檢查出錯時必須 fail-closed（原本只記警告就照常發訊號）。
P0-2 正式發訊與組合回測用同一個組合風險上限、同一套「縮小到放得下」的規則。
P0-3 回測的規模分類與均量取訊號日當時，不用現值。
P1   停損／停利對齊合法升降單位。
P0-4 /healthz 回報部署中的 commit 與策略版本，供核對線上版本。
"""
import math

import backtester as bt
import config
import risk_manager
import scanner
import signal_engine as se


def _sig(ticker="2330.TW", shares=1000, price=100.0, stop=95.0, direction="buy"):
    return {"ticker": ticker, "direction": direction, "entry_price": price, "current_price": price,
            "stop_loss": stop, "suggested_shares": shares, "risk_twd": shares * abs(price - stop)}


# ── P0-1 fail-closed ──
def test_cash_fit_passes_signals_through_when_healthy():
    kept, err = scanner.TWScanEngine._cash_risk_fit([_sig()], [])
    assert err is None and len(kept) == 1


def test_cash_fit_fails_closed_when_the_check_itself_raises(monkeypatch):
    """故障注入：資金檢查函式丟例外 → 本次新訊號全部不發，並回報原因。"""
    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(se, "fit_to_cash", boom)
    kept, err = scanner.TWScanEngine._cash_risk_fit([_sig("A.TW"), _sig("B.TW")], [])
    assert kept == [] and "injected" in err


def test_cash_fit_fails_closed_on_corrupt_position_data():
    """故障注入：既有持倉的風險欄位是壞資料（無法換成數字）→ 不得當成 0 繼續發訊號。"""
    kept, err = scanner.TWScanEngine._cash_risk_fit([_sig()], [{"entry_price": 100, "suggested_shares": 10, "risk_twd": "N/A"}])
    assert kept == [] and err


def test_cash_fit_fails_closed_when_risk_cap_is_unavailable(monkeypatch):
    def boom():
        raise KeyError("MAX_DAILY_RISK")
    monkeypatch.setattr(risk_manager, "open_risk_cap", boom)
    kept, err = scanner.TWScanEngine._cash_risk_fit([_sig()], [])
    assert kept == [] and "KeyError" in err


def test_scan_flow_no_longer_swallows_the_check_and_alerts_instead():
    src = open("scanner.py", encoding="utf-8").read()
    assert 'logger.warning(f"資金上限檢查失敗（不影響其餘流程）' not in src      # 舊的 fail-open 路徑已移除
    flow = src[src.index("final_signals, _fit_err = self._cash_risk_fit("):src.index("shadow.mark_sent(")]
    assert 'add_event("risk"' in flow and "send_alert(" in flow      # 出錯時留事件並通知
    # 檢查在 mark_sent／save_signal 之前；出錯時 final_signals 已被清空，後面的迴圈不會存、不會推
    assert src.index("self._cash_risk_fit(final_signals") < src.index("shadow.mark_sent(") < src.index("store.save_signal(sig)")


def test_fit_shrinks_to_remaining_risk_and_drops_what_cannot_fit():
    cap = config.ACCOUNT_BALANCE_TWD * risk_manager.open_risk_cap()
    held = [{"entry_price": 10.0, "suggested_shares": 100, "risk_twd": cap - 2500}]     # 只剩 2,500 風險額度
    kept, err = scanner.TWScanEngine._cash_risk_fit([_sig("A.TW"), _sig("B.TW")], held)
    assert err is None and [k["ticker"] for k in kept] == ["A.TW"]
    assert kept[0]["suggested_shares"] == 500 and kept[0]["cash_capped"] is True         # 2,500 ÷ 5 元風險


# ── P0-2 單一風險政策 ──
def test_production_and_backtest_share_one_open_risk_cap():
    assert risk_manager.open_risk_cap() == config.MAX_DAILY_RISK == 0.06
    assert "open_risk_cap()" in open("scanner.py", encoding="utf-8").read()
    src = open("backtester.py", encoding="utf-8").read()
    assert "else open_risk_cap())" in src and "else MAX_PORTFOLIO_RISK)" not in src


def test_backtest_fit_matches_production_fit_to_cash():
    for shares, direction, fill, sl, cash, risk_left in (
            (1000, "buy", 100.0, 95.0, 1e9, 2500.0), (1000, "buy", 100.0, 95.0, 30000.0, 1e9),
            (3000, "sell", 100.0, 105.0, 1e9, 12000.0), (1500, "sell", 100.0, 105.0, 1e9, 4000.0),
            (1000, "buy", 100.0, 95.0, 1e9, 1e9), (1000, "buy", 100.0, 95.0, 50.0, 1e9)):
        got = bt._bt_fit_shares(shares, direction, fill, sl, cash, risk_left)
        ref = se.fit_to_cash({"direction": direction, "entry_price": fill, "stop_loss": sl,
                              "suggested_shares": shares}, cash, risk_left)
        assert got == (0 if ref is None else ref["suggested_shares"]), (shares, direction, cash, risk_left)


# ── P0-3 訊號日當時的屬性 ──
def test_asof_size_uses_only_data_up_to_the_signal_day():
    closes = [20.0] * 60 + [800.0] * 40          # 後來才變成高價大量股
    volumes = [300] * 60 + [50000] * 40
    assert bt._asof_size(closes, volumes, 50)[0] == "小型股"       # 20×300×1000×250/1e8 = 15 億
    size, vol = bt._asof_size(closes, volumes, 100)
    assert size == "大型股" and vol == 50000
    # 竄改第 i 根（含）以後的資料，結果不得改變
    c2 = closes[:50] + [9999.0] * 50; v2 = volumes[:50] + [10 ** 9] * 50
    assert bt._asof_size(c2, v2, 50) == bt._asof_size(closes, volumes, 50)
    assert bt._asof_size(closes, volumes, 5, "中型股") == ("中型股", None)   # 資料不足：用後備值、不猜均量


def test_backtests_no_longer_size_with_todays_attributes():
    src = open("backtester.py", encoding="utf-8").read()
    assert 'info.get("volume_lots") if info else None' not in src   # 三個單檔回測不再拿現在的均量
    assert src.count("_asof_size(") >= 5                               # 定義 1 ＋ 組合 1 ＋ 單檔 3


# ── P1 升降單位 ──
def test_tick_size_table_and_rounding():
    assert [se.tick_size(p) for p in (9.99, 10, 49.95, 50, 99.9, 100, 499.5, 500, 999, 1000, 2500)] == \
           [0.01, 0.05, 0.05, 0.1, 0.1, 0.5, 0.5, 1.0, 1.0, 5.0, 5.0]
    assert se.round_to_tick(123.37) == 123.5 and se.round_to_tick(123.37, "down") == 123.0
    assert se.round_to_tick(27.43, "down") == 27.4 and se.round_to_tick(27.43, "up") == 27.45
    assert se.round_to_tick(99.96, "up") == 100.0 and se.round_to_tick(1002.4, "down") == 1000.0
    assert se.round_to_tick(0) == 0


def _on_tick(p):
    t = se.tick_size(p)
    return abs(round(p / t) * t - p) < 1e-6


def test_stops_and_targets_land_on_valid_ticks_without_tightening_the_stop():
    for price, atr in ((27.45, 1.4), (123.7, 4.3), (612.0, 18.5), (1085.0, 33.0), (8.37, 0.31), (55.3, 1.9)):
        for direction in ("buy", "sell"):
            sl = se.calc_stop_loss_tw(direction, price, atr, {}, "中型股")
            tp = se.calc_take_profits_tw(direction, price, sl, "中型股")
            assert _on_tick(sl) and all(_on_tick(tp[k]) for k in ("tp1", "tp2", "tp3")), (price, direction, sl, tp)
            assert abs(price - sl) >= price * 0.015 - 1e-9                 # 對齊後仍不小於最小停損距離
            assert (sl < price) if direction == "buy" else (sl > price)
            raw = price + (1 if direction == "buy" else -1) * abs(price - sl) * config.SWING_PARAMS["中型股"]["tp1_rr"]
            assert (tp["tp1"] <= raw + 1e-9) if direction == "buy" else (tp["tp1"] >= raw - 1e-9)   # 目標不往外推


# ── P0-4 線上版本可核對 ──
def test_healthz_reports_commit_and_strategy_version(monkeypatch):
    import app as app_mod
    import strategy_version as sv
    monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef0123456789abcdef01234567")
    r = app_mod.app.test_client().get("/healthz").get_json()
    assert r["ok"] is True and r["commit"] == "0123456789ab" and r["config_version"] == sv.config_hash()[:12]
    monkeypatch.delenv("RENDER_GIT_COMMIT")
    assert app_mod.app.test_client().get("/healthz").get_json()["commit"] is None      # 取不到就不猜


# ── 影子資料品質門檻（SLO）──
def _ins_shadow(ticker, day, status="pending", kind="candidate", config_ver="abc", expire_days=60, champion_status=None,
                r=None, stop=95.0):
    from state_store import store
    with store._conn() as conn:
        conn.execute("INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,status,"
                     "r_multiple,champion_status,config_ver,expire_days,features,created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (day, ticker, "9999", "t", kind, "buy", 80, 100.0, stop, status, r, champion_status,
                      config_ver, expire_days, "{}", day))


def test_slo_report_flags_missing_lineage_overdue_and_statusless_rows():
    from datetime import datetime
    import shadow
    from state_store import store
    shadow.ensure_table()
    tks = [f"SLO{i}.TW" for i in range(6)]
    def clean():
        with store._conn() as conn:
            for t in tks:
                conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (t,))
    clean()
    try:
        today = datetime(2031, 6, 1)
        base = shadow.slo_report(today=today)["counts"]
        _ins_shadow(tks[0], "2031-05-20")                                        # 正常：未到期
        _ins_shadow(tks[1], "2031-05-20", config_ver=None)                       # 缺策略版本
        _ins_shadow(tks[2], "2031-05-20", expire_days=None)                      # 缺到期天數
        _ins_shadow(tks[3], "2031-03-01")                                        # 60 天到期＋14 天寬限已過仍 pending
        _ins_shadow(tks[4], "2031-04-20")                                        # 42 天：60 天到期者尚未逾期
        _ins_shadow(tks[5], "2031-05-01", status="closed", r=0.5)                # 已結案候選卻沒有 Champion 狀態
        out = shadow.slo_report(today=today)
        d = {k: out["counts"][k] - base[k] for k in out["counts"]}
        assert d["lineage_missing"] == 2 and d["overdue_pending"] == 1 and d["closed_without_status"] == 1
        assert out["ok"] is False and {"lineage_missing", "overdue_pending", "closed_without_status"} <= set(out["breaches"])
    finally:
        clean()


def test_slo_alert_is_wired_into_the_resolve_job():
    src = open("app.py", encoding="utf-8").read()
    blk = src[src.index("shadow.slo_report()"):src.index("job_shadow_resolve slo_report")]
    assert "logger.error" in blk and "shadow_slo_alert_date" in blk and "send_alert" in blk


# ── stale bar：行情過期的標的不產生訊號（fail-closed）──
def test_bar_is_stale_rules():
    f = scanner.TWScanEngine._bar_is_stale
    assert f({"dates": ["2026-11-12", "2026-11-13"]}, "2026-11-13") is False
    assert f({"dates": ["2026-11-11", "2026-11-12"]}, "2026-11-13") is True          # 最後一根停在前一天
    assert f({"dates": ["2026-11-13", "2026-11-14"]}, "2026-11-13") is False         # 比應有日期新（盤中暫定棒）不算過期
    assert f({"dates": []}, "2026-11-13") is True and f({}, "2026-11-13") is True    # 沒有日期：視為過期
    assert f({"dates": ["2026-11-13"]}, scanner.TWScanEngine._STALE_UNKNOWN) is True # 無法判定應有日期：一律過期
    assert f({"dates": ["2020-01-01"]}, None) is False                               # 未設定（非掃描流程的直接呼叫）不判斷


def test_expected_bar_date_follows_trading_calendar_and_fails_closed(monkeypatch):
    import freshness
    from datetime import datetime
    assert freshness.expected_date("13:35", datetime(2026, 11, 13, 16, 10), holiday=lambda d: False) == "2026-11-13"
    assert freshness.expected_date("13:35", datetime(2026, 11, 13, 8, 20), holiday=lambda d: False) == "2026-11-12"   # 盤前
    assert freshness.expected_date("13:35", datetime(2026, 11, 15, 16, 10), holiday=lambda d: False) == "2026-11-13"  # 週日
    def boom(*a, **k):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(freshness, "expected_date", boom)
    assert scanner.TWScanEngine._expected_bar_date() == scanner.TWScanEngine._STALE_UNKNOWN


def _engine(expected):
    import threading
    eng = scanner.TWScanEngine.__new__(scanner.TWScanEngine)
    eng._pre_lock = threading.Lock()
    eng._pre = {"skipped": 0, "passed": 0, "audited": 0, "mismatch": 0, "recon_checked": 0, "recon_fixed": 0,
                "recon_samples": [], "stale_skipped": 0, "stale_samples": []}
    eng._ctrl = []
    eng._exp_bar_date = expected
    return eng


def test_scan_single_skips_stale_ticker_before_any_signal_or_shadow_work(monkeypatch):
    """故障注入：行情來源只給到前一天的 K 棒 → 這檔不評分、不產生訊號、不進影子。"""
    import data_fetcher
    called = {"mtf": 0, "gen": 0}
    stale = {"dates": ["2026-11-11", "2026-11-12"], "closes": [100.0, 101.0], "highs": [101, 102], "lows": [99, 100],
             "opens": [100, 100.5], "volumes": [1000, 1000]}
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": stale)
    monkeypatch.setattr(se, "check_multi_timeframe_tw", lambda *a, **k: called.__setitem__("mtf", called["mtf"] + 1) or {})
    eng = _engine("2026-11-13")
    out = eng._scan_single("9999.TW", {}, None, None,
                           lambda *a, **k: called.__setitem__("gen", called["gen"] + 1) or {"ticker": "9999.TW"},
                           lambda t: {"code": "9999", "name": "測試"})
    assert out is None and called == {"mtf": 0, "gen": 0}
    assert eng._pre["stale_skipped"] == 1 and eng._pre["stale_samples"][0]["last_bar"] == "2026-11-12"


def test_scan_single_still_evaluates_fresh_tickers(monkeypatch):
    import data_fetcher
    called = {"mtf": 0}
    fresh = {"dates": ["2026-11-12", "2026-11-13"], "closes": [100.0, 101.0], "highs": [101, 102], "lows": [99, 100],
             "opens": [100, 100.5], "volumes": [1000, 1000]}
    monkeypatch.setattr(data_fetcher, "fetch_ohlcv", lambda t, tf="daily": fresh)
    def mtf(*a, **k):
        called["mtf"] += 1
        return {"direction": "none", "score": 0}
    monkeypatch.setattr(se, "check_multi_timeframe_tw", mtf)
    eng = _engine("2026-11-13")
    eng._scan_single("9999.TW", {}, None, None, lambda *a, **k: None, lambda t: {"code": "9999", "name": "測試"})
    assert called["mtf"] == 1 and eng._pre["stale_skipped"] == 0


# ── 尾端風險：連續跌停情境 ──
def test_portfolio_risk_reports_consecutive_limit_down_scenarios():
    rows = [{"code": "A", "direction": "buy", "sector": "半導體業", "entry_price": 100.0, "stop_loss": 95.0, "suggested_shares": 2000},
            {"code": "B", "direction": "buy", "sector": "航運業", "entry_price": 50.0, "stop_loss": 47.5, "suggested_shares": 2000}]
    out = risk_manager.portfolio_risk(rows, balance=1_000_000)
    g = {x["limit_days"]: x for x in out["gap_scenarios"]}
    assert g[1]["all_pnl_twd"] == -30000 and g[2]["all_pnl_twd"] == -57000 and g[3]["all_pnl_twd"] == -81300
    assert g[1]["top_sector"] == "半導體業" and g[1]["top_sector_pnl_twd"] == -20000
    assert out["worst_case_loss"] == 15000 and g[1]["vs_stop_loss_x"] == 2.0     # 一天跌停就是「全部停損」的兩倍
    assert "連續跌停" in out["gap_basis"]
    assert risk_manager.portfolio_risk([], balance=1_000_000)["n"] == 0


# ── 回測結果帶策略版本 ──
def test_backtest_reports_the_strategy_version_it_ran_under():
    import strategy_version as sv
    assert bt._bt_config_version() == sv.config_hash()[:12]
    assert '"config_version": _bt_config_version()' in open("backtester.py", encoding="utf-8").read()


def test_lineage_summary_groups_recent_rows_by_version():
    import shadow
    from state_store import store
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker IN ('LS1.TW','LS2.TW')")
    try:
        _ins_shadow("LS1.TW", "2031-07-01", config_ver="verA", expire_days=60)
        _ins_shadow("LS2.TW", "2031-07-01", config_ver="verA", expire_days=60)
        got = [x for x in shadow.lineage_summary(since="2031-07-01") if x["bar_date"] == "2031-07-01"]
        assert got == [{"bar_date": "2031-07-01", "config_ver": "verA", "expire_days": 60, "kind": "candidate", "n": 2}]
    finally:
        with store._conn() as conn:
            conn.execute("DELETE FROM shadow_signals WHERE ticker IN ('LS1.TW','LS2.TW')")


def test_slo_flags_a_trading_day_with_no_shadow_rows(monkeypatch):
    """2026-11-11 那種「盤後掃描被部署中斷、整天沒有紀錄」要被抓到；週末與尚未掃描的今天不算。"""
    from datetime import datetime
    import freshness
    import shadow
    from state_store import store
    shadow.ensure_table()
    monkeypatch.setattr(shadow, "LINEAGE_SINCE", "2031-08-01")
    monkeypatch.setattr(freshness, "_is_holiday", lambda d: False)
    days = ["2031-08-04", "2031-08-05", "2031-08-07"]          # 週一、週二、週四；週三 08-06 缺
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker LIKE 'MISS%'")
    try:
        for i, d in enumerate(days):
            _ins_shadow(f"MISS{i}.TW", d)
        out = shadow.slo_report(today=datetime(2031, 8, 8, 10, 0))   # 週五盤中：今天的掃描還沒跑，不算缺
        assert out["missing_days"] == ["2031-08-01", "2031-08-06"] and out["counts"]["missing_trading_days"] == 2
        out = shadow.slo_report(today=datetime(2031, 8, 9, 10, 0))   # 週六：週五的掃描應已完成 → 也算缺
        assert "2031-08-08" in out["missing_days"] and "2031-08-02" not in out["missing_days"]   # 08-02 是週六
    finally:
        with store._conn() as conn:
            conn.execute("DELETE FROM shadow_signals WHERE ticker LIKE 'MISS%'")
