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
