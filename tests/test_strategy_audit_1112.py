"""2026-11-12 正式策略程式碼檢查：找到並修正的缺陷（皆為邏輯／資料正確性，不調任何門檻）。

1) 大盤資料抓取逾時 → 原本以空字典繼續，can_trade 預設 True，做多訊號在「完全不知道大盤狀況」下照發。
2) 盤前 08:20 訊號的結算從『隔天』才開始，跳過實際進場的那一天。
3) 正式訊號到期時資料停更 → 原本直接以舊收盤價結算。
4) 分數被籌碼／品質／基本面調整後，評級沒有重算。
5) 大盤重挫日連影子追蹤都不記錄（且日誌寫「只掃空單」，實際多空皆停）。
6) resonance 回傳的是「有沒有小時線資料」，不是共振結果。
"""
import re

import scanner as scanner_mod
import signal_engine as se
from scanner import TWScanEngine


# ── 2) 盤前訊號的結算基準日 ──
def test_premarket_signal_settles_from_its_own_trading_day():
    """08:20（臺北）盤前訊號＝UTC 00:20 同一天：訊號日應為前一日，當天 K 棒才會被納入結算。"""
    assert TWScanEngine._signal_ref_date({"generated_at": "2026-11-11T00:20:00+00:00"}) == "2026-11-10"
    # 舊算法 generated_at[:10]＝2026-11-11 → 結算只看 11-12 以後，11-11（實際進場日）整天被跳過


def test_close_scan_signal_keeps_same_day_as_signal_date():
    """16:30（臺北）盤後訊號＝UTC 08:30：訊號日＝當天，當天 K 棒不得當成訊號後行情。"""
    assert TWScanEngine._signal_ref_date({"generated_at": "2026-11-11T08:30:00+00:00"}) == "2026-11-11"
    # 盤中手動掃描（臺北 12:00）：當天 K 棒已在訊號前發生一部分，保守視為當天
    assert TWScanEngine._signal_ref_date({"generated_at": "2026-11-11T04:00:00+00:00"}) == "2026-11-11"
    # 臺北凌晨 07:00（UTC 前一日 23:00）：仍以臺北日期判定
    assert TWScanEngine._signal_ref_date({"generated_at": "2026-11-10T23:00:00+00:00"}) == "2026-11-10"
    assert TWScanEngine._signal_ref_date({"generated_at": "2026-11-11T08:30:00"}) == "2026-11-11"   # 無時區＝UTC
    assert TWScanEngine._signal_ref_date({"generated_at": "bad"}) == "bad"[:10]                      # 壞值不丟例外


# ── 4) 評級依最終分數 ──
def test_grade_fields_single_source():
    assert se.grade_fields(85)["grade"] == "A" and se.grade_fields(84)["grade"] == "B"
    assert se.grade_fields(75)["grade"] == "B" and se.grade_fields(74)["grade"] == "C"
    assert "強力" in se.grade_fields(90)["action"] and se.grade_fields(70)["emoji_grade"] == "⚪"


def test_filter_and_rank_regrades_after_score_adjustments():
    src = open(scanner_mod.__file__, encoding="utf-8").read()
    body = src[src.index("def _filter_and_rank"):]
    body = body[:body.index("\n    def ", 10)]
    assert "grade_fields(_sig.get(\"score\") or 0)" in body
    assert body.index("grade_fields(_sig") > body.index("combined.sort(")      # 在所有調整與排序之後


# ── 6) resonance 是共振結果，不是「有小時線資料」 ──
def _tf(trend="up", n=260):
    step = 0.4 if trend == "up" else -0.4
    base = 100 if trend == "up" else 300          # 下跌序列從 300 起算，避免跌成負價
    closes = [base + i * step for i in range(n)]
    return {"closes": closes, "highs": [c + 1 for c in closes], "lows": [c - 1 for c in closes],
            "opens": [c - step / 2 for c in closes], "volumes": [1000 + (i % 5) * 50 for i in range(n)],
            "current_price": closes[-1]}


def test_resonance_reports_actual_alignment_not_data_presence():
    up = se.check_multi_timeframe_tw({"daily": _tf("up"), "hourly": _tf("up")})
    against = se.check_multi_timeframe_tw({"daily": _tf("up"), "hourly": _tf("down")})
    none = se.check_multi_timeframe_tw({"daily": _tf("up")})
    assert up["direction"] == "buy" and against["direction"] == "buy"
    assert up["has_hourly"] is True and against["has_hourly"] is True and none["has_hourly"] is False
    assert up["resonance"] is True and against["resonance"] is False and none["resonance"] is False
    assert up["score"] == against["score"] + 10          # +10 的判定本身未變，只是回傳值改為真實結果


# ── 1) 大盤資料逾時 fail-closed；5) 重挫日影子照記、正式訊號照停 ──
def _stock():
    return {"name": "測試", "sector": "半導體業", "size_cat": "中型股", "code": "9999", "volume_lots": 99999}


def test_market_data_error_produces_no_signal_and_no_shadow(monkeypatch):
    import shadow
    calls = []
    monkeypatch.setattr(shadow, "note_reject", lambda *a, **k: calls.append(a[7]))
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "control_from_engine", lambda *a, **k: None)
    mo = {"can_trade": False, "market_status": "data_error"}
    assert se.generate_signal_tw("9999.TW", _stock(), {"daily": _tf("up")}, mo) is None
    assert calls == []                                    # 沒有可信的市場狀態：不評分、不記假想單


def test_market_stop_day_records_shadow_reject_but_sends_nothing(monkeypatch):
    import shadow
    calls = []
    monkeypatch.setattr(shadow, "note_reject", lambda *a, **k: calls.append(a[7]))
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "control_from_engine", lambda *a, **k: None)
    monkeypatch.setattr(se, "check_multi_timeframe_tw",
                        lambda tf, disabled_factors=None: {"direction": "buy", "score": 90, "adx_value": 30,
                                                           "vol_ratio": 2.0, "weekly_bias": "bullish",
                                                           "rsi_value": 60, "conditions_met": [], "conditions_fail": []})
    mo = {"can_trade": False, "market_status": "stop"}
    assert se.generate_signal_tw("9999.TW", _stock(), {"daily": _tf("up")}, mo) is None     # 正式訊號照停
    assert calls == ["market_stop"]                                                          # 影子照記
    assert "market_stop" in shadow.REASONS


def test_scan_treats_market_overview_timeout_as_data_error():
    src = open(scanner_mod.__file__, encoding="utf-8").read()
    m = re.search(r"except concurrent\.futures\.TimeoutError:.*?market_overview = (\{.*?\})\n", src, re.S)
    assert m and '"can_trade": False' in m.group(1) and '"market_status": "data_error"' in m.group(1)
    assert '本次暫停所有新訊號（多空皆停）' in src and 'logger.warning(f"大盤重挫 {twii_chg:.1f}%，本次只掃空單")' not in src


# ── 3) 正式訊號：到期但資料停更 ──
def test_real_signal_settlement_waits_for_fresh_bar_then_flags_stale():
    src = open(scanner_mod.__file__, encoding="utf-8").read()
    body = src[src.index("def _resolve_pending_signals"):src.index("def check_intraday_price_alerts")]
    assert "gen_date = self._signal_ref_date(sig)" in body
    assert "staged_open_at_end(sim, gen_date, expire_days)" in body
    assert "_STALE_PRICE_GRACE_DAYS" in body and "資料停更" in body
    assert TWScanEngine._STALE_PRICE_GRACE_DAYS == 7


# ── E03：重挫日放空先做影子驗證（使用者 2026-11-12 表示要開放；正式訊號維持不發） ──
def _short_env(monkeypatch, weekly="bearish"):
    import shadow, event_filter
    calls = []
    monkeypatch.setattr(shadow, "note_reject", lambda *a, **k: calls.append((a[7], a[9] if len(a) > 9 else k.get("sl"))))
    monkeypatch.setattr(shadow, "observe", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "control_from_engine", lambda *a, **k: None)
    monkeypatch.setattr(event_filter, "check", lambda code, within_days=None: {})
    monkeypatch.setattr(se, "check_multi_timeframe_tw",
                        lambda tf, disabled_factors=None: {"direction": "sell", "score": 90, "adx_value": 35,
                                                           "adx_bias": "bearish", "vol_ratio": 2.5,
                                                           "weekly_bias": weekly, "rsi_value": 35,
                                                           "conditions_met": [], "conditions_fail": []})
    # 部位固定給 2 張（放空須整張；零股規則另由 test_executability 驗證）
    monkeypatch.setattr(se, "calc_position_size",
                        lambda p, sl, **k: {"shares": 2000, "lots": 2.0, "risk_twd": 1000, "risk_pct": 0.2,
                                            "position_value": 2000 * p, "roundtrip_cost": 100, "breakeven_pct": 0.1})
    mo = {"can_trade": False, "market_status": "stop", "sentiment_score": 20,
          "regime": {"available": True, "trend": "bearish"}}
    return calls, mo


def test_crash_day_short_passing_all_gates_is_recorded_as_market_stop_hypothetical(monkeypatch):
    """通過全部放空關卡者，最後才以 market_stop 記成假想單（帶實際停損價）；正式訊號仍不產生。"""
    calls, mo = _short_env(monkeypatch)
    assert se.generate_signal_tw("9999.TW", _stock(), {"daily": _tf("down")}, mo) is None
    assert [c[0] for c in calls] == ["market_stop"]
    assert calls[0][1] is not None                       # 走到了停損／部位計算之後才擋下


def test_crash_day_short_failing_a_gate_keeps_its_own_reason(monkeypatch):
    """沒通過放空關卡者照原本的原因記錄，不混進 market_stop——E03 的樣本才精確等於「真的會發出的單」。"""
    calls, mo = _short_env(monkeypatch, weekly="bullish")
    assert se.generate_signal_tw("9999.TW", _stock(), {"daily": _tf("down")}, mo) is None
    assert [c[0] for c in calls] == ["gate_weekly"]


def test_e03_blocked_rule_experiment_is_preregistered_and_shadow_only():
    import experiments as ex
    e = ex.BLOCKED_REGISTRY[0]
    doc = open(e["doc"], encoding="utf-8").read()
    assert e["id"] == "E03" and "id: E03" in doc and "正式訊號維持" in doc
    assert f"min_oos_days: {e['min_oos_days']}" in doc and f"min_oos_trades: {e['min_oos_trades']}" in doc

    def row(day, r, reason="market_stop", direction="sell", kind="rejected", status="final"):
        return {"bar_date": day, "kind": kind, "direction": direction, "status": "closed", "champion_r": r,
                "champion_status": status, "f": {"reject": reason}}
    few = [row("2026-12-01", 1.0), row("2026-12-01", 0.5)]
    assert ex.evaluate_blocked("E03", few)["oos"]["verdict"] == "insufficient"      # 重挫日太少：不下結論
    good = [row(f"2027-{1 + i // 28:02d}-{i % 28 + 1:02d}", 0.8 + (0.1 if j % 2 else -0.1))
            for i in range(10) for j in range(4)]
    noise = [row("2027-01-01", -9.0, reason="adx_low"), row("2027-01-01", -9.0, direction="buy"),
             row("2027-01-01", -9.0, kind="candidate"), row("2027-01-01", -9.0, status="provisional")]
    o = ex.evaluate_blocked("E03", good + noise)["oos"]
    assert o["n_final"] == 40 and o["n_days"] == 10 and o["avg_r"] == 0.8           # 只算 market_stop＋放空＋已定案
    assert o["verdict"] == "supported" and "不自動開放" in o["verdict_note"]
    assert ex.evaluate_blocked("E03", [row("2026-10-20", 5.0)] * 50)["exploratory"]["verdict"] == "exploratory_only"
