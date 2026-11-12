"""2026-11-12 Champion 變更：放空停發、做多須週線偏多、做多門檻 75；被擋下的照樣進影子追蹤。

依據 docs/research/CHAMPION_REPLAY_2020_2026.md：把正式訊號引擎重播 2020–2026（11,759 筆），
先在 2020–2023 列規則、再到 2024–2026 驗證，這三類訊號在兩個期間的淨 R 皆顯著為負。
"""
import backtester as bt
import config
import shadow
import signal_engine as se
import strategy_version as sv


def _daily(trend="up", n=260):
    step = 0.4 if trend == "up" else -0.4
    base = 100 if trend == "up" else 300
    cl = [base + i * step for i in range(n)]
    return {"closes": cl, "highs": [c + 1 for c in cl], "lows": [c - 1 for c in cl],
            "opens": [c - step / 2 for c in cl], "volumes": [1000 + (i % 5) * 50 for i in range(n)],
            "current_price": cl[-1]}


_STOCK = {"name": "測試", "sector": "半導體業", "size_cat": "中型股", "code": "9999", "volume_lots": 99999}


def _run(monkeypatch, direction, score, weekly, **mo_extra):
    import event_filter
    calls = []
    monkeypatch.setattr(shadow, "note_reject", lambda *a, **k: calls.append((a[7], a[9] if len(a) > 9 else None)))
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "control_from_engine", lambda *a, **k: None)
    monkeypatch.setattr(event_filter, "check", lambda code, within_days=None: {})
    monkeypatch.setattr(se, "check_multi_timeframe_tw",
                        lambda tf, disabled_factors=None: {
                            "direction": direction, "score": score, "adx_value": 35,
                            "adx_bias": "bearish" if direction == "sell" else "bullish", "vol_ratio": 2.5,
                            "weekly_bias": weekly, "rsi_value": 55, "conditions_met": [], "conditions_fail": []})
    monkeypatch.setattr(se, "calc_position_size",
                        lambda p, sl, **k: {"shares": 2000, "lots": 2.0, "risk_twd": 1000, "risk_pct": 0.2,
                                            "position_value": 2000 * p, "roundtrip_cost": 100, "breakeven_pct": 0.1})
    mo = {"can_trade": True, "market_status": "normal", "sentiment_score": 30,
          "regime": {"available": True, "trend": "bearish" if direction == "sell" else "bullish"}}
    mo.update(mo_extra)
    sig = se.generate_signal_tw("9999.TW", _STOCK, {"daily": _daily("down" if direction == "sell" else "up")}, mo)
    return sig, calls


def test_config_and_hash_reflect_the_change():
    assert config.ENABLE_SHORT_SIGNALS is False
    assert config.LONG_REQUIRE_WEEKLY_BULLISH is True and config.LONG_SIGNAL_THRESH["min_score"] == 75
    assert config.SIGNAL_THRESHOLDS["min_score"] == 65            # 保留為影子觀察下限
    assert {"LONG_SIGNAL_THRESH", "LONG_REQUIRE_WEEKLY_BULLISH", "ENABLE_SHORT_SIGNALS"} <= set(sv._TRACKED)
    assert sv.config_hash().startswith("0c49884d59de")            # 目前的 Champion 版本（含同日的到期 15→60 天）
    assert se.min_score_for("buy") == 75 and se.min_score_for("sell") == 65


def test_long_passes_only_with_weekly_bullish_and_score_75(monkeypatch):
    sig, calls = _run(monkeypatch, "buy", 80, "bullish")
    assert sig and sig["direction"] == "buy" and calls == []
    sig, calls = _run(monkeypatch, "buy", 75, "strong_bullish")
    assert sig and calls == []                                    # 門檻含 75；strong_bullish 也算偏多


def test_long_against_weekly_trend_is_blocked_but_tracked(monkeypatch):
    for weekly in ("bearish", "neutral"):
        sig, calls = _run(monkeypatch, "buy", 90, weekly)
        assert sig is None and [c[0] for c in calls] == ["gate_weekly_long"]   # 分數再高也擋；影子續追
        assert calls[0][1] is not None        # 走完其餘全部關卡（含停損計算）才記：＝若取消此條件真的會發出的單


def test_long_below_75_is_blocked_but_tracked(monkeypatch):
    sig, calls = _run(monkeypatch, "buy", 70, "bullish")
    assert sig is None and [c[0] for c in calls] == ["long_score"]
    assert calls[0][1] is not None
    sig, calls = _run(monkeypatch, "buy", 70, "neutral")
    assert sig is None and [c[0] for c in calls] == ["gate_weekly_long"]   # 兩者皆不符：一筆只記一個原因（週線優先）


def test_blocked_long_that_fails_another_gate_keeps_that_gates_reason(monkeypatch):
    """被停發類別的做多若本來就過不了別的關卡（例如盈虧比），維持那個關卡的原因，不混進停發類別。"""
    monkeypatch.setattr(se, "calc_take_profits_tw", lambda *a, **k: {"rr1": 0.1, "rr2": 0.2, "rr3": 0.3,
                                                                      "tp1": 1, "tp2": 2, "tp3": 3})
    sig, calls = _run(monkeypatch, "buy", 70, "bullish")
    assert sig is None and [c[0] for c in calls] == ["rr_low"]


def test_short_is_not_sent_but_gate_passers_are_tracked_as_short_off(monkeypatch):
    sig, calls = _run(monkeypatch, "sell", 90, "bearish")
    assert sig is None and [c[0] for c in calls] == ["short_off"]
    assert calls[0][1] is not None            # 走完全部放空關卡（含停損計算）才記：＝若開放真的會發出的單
    sig, calls = _run(monkeypatch, "sell", 90, "bullish")
    assert sig is None and [c[0] for c in calls] == ["gate_weekly"]      # 沒過關者維持原本的原因，不混進 short_off


def test_shorts_work_again_if_reenabled(monkeypatch):
    monkeypatch.setattr(se, "ENABLE_SHORT_SIGNALS", True)
    sig, calls = _run(monkeypatch, "sell", 90, "bearish")
    assert sig and sig["direction"] == "sell" and calls == []     # 關卡與門檻完整保留，可依證據恢復


def test_new_reject_reasons_are_labelled_for_rule_attribution():
    for k in ("short_off", "gate_weekly_long", "long_score"):
        assert k in shadow.REASONS


def test_rank_stage_refilters_with_direction_threshold():
    src = open("scanner.py", encoding="utf-8").read()
    assert src.count('if s["score"] >= _min_score_for(s.get("direction"))') == 3
    assert 'if s["score"] >= THRESH["min_score"]]' not in src     # 不再用影子觀察下限（65）過濾正式訊號


def test_backtests_follow_the_same_gates():
    assert bt._champion_gate("buy", 80, {"weekly_bias": "bullish"}) is True
    assert bt._champion_gate("buy", 74, {"weekly_bias": "bullish"}) is False
    assert bt._champion_gate("buy", 90, {"weekly_bias": "bearish"}) is False
    assert bt._champion_gate("buy", 90, {}) is False               # 沒有週線資料＝無法確認偏多：不進場
    assert bt._champion_gate("sell", 90, {"weekly_bias": "bearish"}) is False
    assert bt._champion_gate("sell", 90, {"weekly_bias": "bearish"}, short_research=True) is True


def test_weekly_asof_slice_excludes_unfinished_week():
    w = {"dates": [f"2026-{m:02d}-{d:02d}" for m in range(1, 7) for d in (5, 12, 19, 26)],
         "closes": list(range(24)), "highs": list(range(24)), "lows": list(range(24)),
         "opens": list(range(24)), "volumes": list(range(24))}
    s = bt._weekly_asof_slice(w, "2026-06-20")           # 6/19 那週還在進行：不得納入
    assert s and len(s["closes"]) == 22 and s["closes"][-1] == 21
    assert bt._weekly_asof_slice(w, "2026-02-01") is None and bt._weekly_asof_slice(None, "2026-06-20") is None


def test_blocked_classes_have_preregistered_restoration_watch():
    """停發的三類各有一個預先登錄的「恢復觀察」實驗：只看 11-13 起的假想單，條件事前寫死、不自動恢復。"""
    import experiments as ex
    by_reason = {e["reject_reason"]: e for e in ex.BLOCKED_REGISTRY}
    for reason, eid, direction in (("short_off", "E04", "sell"), ("gate_weekly_long", "E05", "buy"),
                                   ("long_score", "E06", "buy")):
        e = by_reason[reason]
        doc = open(e["doc"], encoding="utf-8").read()
        assert e["id"] == eid and e["direction"] == direction and f"id: {eid}" in doc
        assert f"min_oos_days: {e['min_oos_days']}" in doc and f"min_oos_trades: {e['min_oos_trades']}" in doc
        assert e["min_oos_days"] == ex.MIN_OOS_DAYS and e["min_oos_trades"] == ex.MIN_OOS_TRADES
        assert "正式訊號維持停發" in doc and "size = 10" in doc

    def row(day, r, reason, direction):
        return {"bar_date": day, "kind": "rejected", "direction": direction, "status": "closed", "champion_r": r,
                "champion_status": "final", "f": {"reject": reason}}
    days = [f"2027-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(45)]
    losing = [row(d, -0.3 + (0.05 if j % 2 else -0.05), "long_score", "buy") for d in days for j in range(6)]
    o = ex.evaluate_blocked("E06", losing)["oos"]
    assert o["n_final"] == 270 and o["verdict"] == "not_supported" and "維持現狀" in o["verdict_note"]
    few = losing[:30]
    o = ex.evaluate_blocked("E06", few)["oos"]
    assert o["verdict"] == "insufficient" and "重挫日" not in o["verdict_note"]      # 措辭不沿用 E03 的重挫日
    winning = [row(d, 0.4 + (0.05 if j % 2 else -0.05), "short_off", "sell") for d in days for j in range(6)]
    o = ex.evaluate_blocked("E04", winning + losing)["oos"]                          # 不同原因互不混入
    assert o["n_final"] == 270 and o["verdict"] == "supported" and "不自動開放" in o["verdict_note"]
    assert ex.evaluate_blocked("E04", [row("2026-11-10", 5.0, "short_off", "sell")] * 300)["exploratory"]["verdict"] == "exploratory_only"
    assert len(ex.REGISTRY) + len(ex.BLOCKED_REGISTRY) + len(ex.ALT_EXIT_REGISTRY) == 10   # family_size（各定義檔同步寫 size = 10）
