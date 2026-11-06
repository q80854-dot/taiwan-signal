"""AI 解釋訊號／當日摘要（#5）測試。確保：只用真實欄位、缺資料標暫無、不虛構數字、
做空風險誠實揭露、市場資料異常時摘要不假裝正常。"""
import explain


def _sig(**kw):
    base = {
        "code": "2330", "name": "台積電", "direction": "buy", "grade": "A", "score": 88,
        "weekly_bias": "strong_bullish", "adx_value": 32, "rsi_value": 62, "vol_ratio": 1.8,
        "inst_signal": "外資買超 +5,000張", "conditions_met": ["EMA多頭排列 ✓", "站上半年線 ✓"],
        "conditions_fail": [], "risk_twd": 12000, "risk_pct": 1.2, "rr1": 1.5,
        # DB signals 列：欄位名 suggested_lots 但實際存「股數」（2500 股＝2.5 張）
        "sl_pct": 4.0, "tp1_pct": 6.0, "suggested_lots": 2500, "reason_brief": "多頭確認",
    }
    base.update(kw)
    return base


def test_explain_buy_lists_supporting_factors():
    out = explain.explain_signal_text(_sig())
    assert out["ok"] is True
    assert out["grade"] == "A" and out["score"] == 88
    joined = " ".join(out["why"])
    assert "ADX 32" in joined and "趨勢明確" in joined
    assert "RSI 62" in joined and "多方動能" in joined
    assert "量比 1.8x" in joined
    assert "外資買超" in joined
    assert "EMA多頭排列" in joined and "✓" not in joined          # 勾勾已清掉
    assert "風報比 1:1.5" in out["risk"] and "本金 1.2%" in out["risk"]


def test_explain_shares_unit_not_1000x_inflated():
    """單位稽核：DB 列 suggested_lots 實存股數（2500 股）；不得顯示成「2500 張」。"""
    out = explain.explain_signal_text(_sig(suggested_lots=2500))
    assert "2,500 股" in out["risk"] and "約 2.5 張" in out["risk"]
    assert "2500 張" not in out["risk"] and "2,500 張" not in out["risk"]


def test_explain_prefers_suggested_shares_when_present():
    """記憶體訊號 dict：suggested_shares 才是股數、suggested_lots 是張數——優先取 shares。"""
    out = explain.explain_signal_text(_sig(suggested_shares=3000, suggested_lots=3))
    assert "3,000 股" in out["risk"] and "約 3.0 張" in out["risk"]


def test_explain_missing_indicators_not_fabricated():
    """缺 ADX／RSI／量比 → 不臆造，只是不列入 why，不會出現假數字。"""
    s = _sig(adx_value=None, rsi_value=None, vol_ratio=None, inst_signal="",
             conditions_met=[])
    out = explain.explain_signal_text(s)
    joined = " ".join(out["why"])
    assert "ADX" not in joined and "RSI" not in joined and "量比" not in joined


def test_explain_high_chase_and_adverse_inst_in_watch():
    s = _sig(chase_level="high", ext_atr=3.2, inst_signal="外資賣超 -3,000張 ⚠️")
    out = explain.explain_signal_text(s)
    watch = " ".join(out["watch"])
    assert "追高風險高" in watch and "3.2 ATR" in watch
    assert "法人動向與方向相悖" in watch


def test_explain_short_research_discloses_borrow_risk():
    s = _sig(direction="sell", grade="B", executable=False, weekly_bias="bearish",
             inst_signal="")
    out = explain.explain_signal_text(s)
    watch = " ".join(out["watch"])
    assert "研究版放空" in watch and "借券" in watch


def test_explain_data_missing_caveats():
    s = _sig(revenue_check="data_missing", quality_check="unavailable", news_check="unavailable")
    out = explain.explain_signal_text(s)
    watch = " ".join(out["watch"])
    assert "月營收資料本次無法取得" in watch
    assert "獲利品質資料本次無法取得" in watch
    assert "重大訊息公告本次無法取得" in watch


def test_summary_counts_and_posture():
    sigs = [_sig(code="2330", score=88, grade="A"),
            _sig(code="2317", name="鴻海", score=72, grade="B"),
            _sig(code="3008", name="大立光", direction="sell", score=65, grade="B")]
    mo = {"index": {"twii": {"chg": 0.8, "source": "ok"}, "vix": {"price": 16.2, "source": "ok"}},
          "foreign": {"net_buy_twd": 8.5e9}, "sentiment_score": 62, "market_status": "normal"}
    out = explain.daily_summary_text(sigs, mo, {"scanned": 180})
    assert out["ok"] is True
    assert out["counts"] == {"signals": 3, "buy": 2, "sell": 1, "scanned": 180}
    assert "掃描 180 檔" in out["headline"] and "做多 2" in out["headline"]
    assert "加權指數 ▲0.80%" in out["market"] and "VIX 16.2" in out["market"]
    assert "外資買超 85.0 億" in out["market"]
    assert "市場情緒偏多" in out["market"]
    assert "A 級 1" in " ".join(out["points"])
    assert out["posture"].startswith("明日方向：中性觀察")   # 62 分 → 中性


def test_summary_vix_missing_flags_incomplete():
    """VIX 缺失（大盤看似正常）→ 市場標暫無、並提示判斷不完整（對齊 risk_manager 的誠實原則）。"""
    mo = {"index": {"twii": {"chg": 0.3, "source": "ok"}, "vix": {"source": "error"}},
          "foreign": {"net_buy_twd": 0}, "sentiment_score": 55, "market_status": "normal"}
    out = explain.daily_summary_text([], mo, {"scanned": 150})
    assert "VIX：暫無" in out["market"]
    assert any("VIX 缺失" in c for c in out["caveats"])
    assert "今日無符合條件的新訊號" in " ".join(out["points"])


def test_summary_market_data_error_no_fake_normal():
    mo = {"index": {"twii": {"source": "error"}, "vix": {"source": "error"}},
          "foreign": {}, "market_status": "data_error"}
    out = explain.daily_summary_text([], mo, {})
    assert "加權指數：暫無" in out["market"]
    assert out["counts"]["scanned"] is None
    assert any("大盤指數資料異常" in c for c in out["caveats"])


def test_explain_rejects_non_signal():
    assert explain.explain_signal_text({}).get("ok") is False
    assert explain.explain_signal_text(None).get("ok") is False
