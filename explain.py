"""AI 解釋訊號／當日摘要（2026-11-04，大型功能 #5）。

設計原則（對齊本系統鐵則）：
  · 不接外部 LLM、不需金鑰、不依賴外網、可離線、CI 可測。
  · 只根據訊號／大盤「真實欄位」組出人話說明——零幻覺、不虛構任何數字。
  · 缺資料一律標「暫無」，不臆測（correct-or-暫無）。
  · 這是「把系統已知的事實翻成白話」，不是新的投資判斷；每則結尾附免責。

兩個進入點：
  · explain_signal_text(sig)  → 單檔訊號為何成立（支持因子＋須留意＋風險條件）。
  · daily_summary_text(signals, market_overview, scan_stats) → 當日掃描白話摘要。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

_TPE = timezone(timedelta(hours=8))

_DISCLAIMER = "本說明由系統依訊號既有資料自動整理，僅解釋評分依據，非投資建議。"

_WEEKLY_ZH = {
    "strong_bullish": "週線強勢多頭",
    "bullish": "週線多頭",
    "neutral": "週線橫盤整理",
    "bearish": "週線空頭",
    "strong_bearish": "週線強勢空頭",
}


def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _adx_phrase(adx: Optional[float]) -> Optional[str]:
    if adx is None:
        return None
    if adx >= 40:
        return f"ADX {adx:.0f}：趨勢極強"
    if adx >= 25:
        return f"ADX {adx:.0f}：趨勢明確"
    if adx >= 20:
        return f"ADX {adx:.0f}：趨勢偏弱、剛成形"
    return f"ADX {adx:.0f}：盤整、趨勢不明確"


def _rsi_phrase(rsi: Optional[float], is_buy: bool) -> Optional[str]:
    if rsi is None:
        return None
    if is_buy:
        if rsi >= 70:
            return f"RSI {rsi:.0f}：偏超買，追高須留意回檔"
        if rsi >= 50:
            return f"RSI {rsi:.0f}：多方動能佔優"
        return f"RSI {rsi:.0f}：多方動能尚未轉強"
    # 做空鏡像
    if rsi <= 30:
        return f"RSI {rsi:.0f}：偏超賣，搶空須留意反彈"
    if rsi <= 50:
        return f"RSI {rsi:.0f}：空方動能佔優"
    return f"RSI {rsi:.0f}：空方動能尚未轉強"


def _vol_phrase(vr: Optional[float]) -> Optional[str]:
    if vr is None:
        return None
    if vr >= 2.0:
        return f"量比 {vr:.1f}x：爆量，資金明顯進駐"
    if vr >= 1.5:
        return f"量比 {vr:.1f}x：量能放大、有量有價"
    if vr >= 1.0:
        return f"量比 {vr:.1f}x：量能持平"
    if vr < 0.7:
        return f"量比 {vr:.1f}x：量縮，動能不足須留意"
    return f"量比 {vr:.1f}x：量能略縮"


def explain_signal_text(sig: Dict) -> Dict:
    """把一檔訊號的評分依據翻成白話。只用 sig 既有欄位，缺則略過或標暫無。"""
    if not isinstance(sig, dict) or not sig.get("code"):
        return {"ok": False, "reason": "no_signal"}
    is_buy = sig.get("direction") == "buy"
    dir_zh = "做多" if is_buy else "做空"
    grade = sig.get("grade", "C")
    score = sig.get("score")
    score_str = f"{score} 分" if score is not None else "暫無評分"

    headline = f"{grade} 級{dir_zh}訊號，技術面評分 {score_str}"

    # ── 支持因子（為何成立）──
    why: List[str] = []
    wk = _WEEKLY_ZH.get(sig.get("weekly_bias"))
    if wk and sig.get("weekly_bias") != "neutral":
        # 週線與方向一致才算支持；橫盤不列為支持理由
        aligned = (is_buy and "bullish" in (sig.get("weekly_bias") or "")) or \
                  ((not is_buy) and "bearish" in (sig.get("weekly_bias") or ""))
        if aligned:
            why.append(f"{wk}，與{dir_zh}方向一致")
    for p in (_adx_phrase(_num(sig.get("adx_value"))),
              _rsi_phrase(_num(sig.get("rsi_value")), is_buy),
              _vol_phrase(_num(sig.get("vol_ratio")))):
        if p:
            why.append(p)
    inst = (sig.get("inst_signal") or "").strip()
    if inst and "⚠️" not in inst:
        why.append(f"法人動向：{inst}")
    # 條件成立清單（signal_engine 已是人話，直接帶入、去掉尾端勾勾符號）
    for c in (sig.get("conditions_met") or [])[:6]:
        txt = str(c).replace(" ✓", "").replace("✓", "").strip()
        if txt:
            why.append(txt)

    # ── 須留意（扣分／風險條件）──
    watch: List[str] = []
    chase = sig.get("chase_level")
    ext = _num(sig.get("ext_atr"))
    if chase == "high":
        watch.append(f"追高風險高：距 20 日均線約 {ext:.1f} ATR，位置偏高" if ext is not None
                     else "追高風險高：進場位置偏延伸")
    elif chase == "mid":
        watch.append(f"追高風險中等：距 20 日均線約 {ext:.1f} ATR" if ext is not None
                     else "追高風險中等")
    inst_warn = (sig.get("inst_signal") or "")
    if "⚠️" in inst_warn:
        watch.append(f"法人動向與方向相悖：{inst_warn.replace('⚠️', '').strip()}")
    for c in (sig.get("conditions_fail") or [])[:4]:
        txt = str(c).strip()
        if txt:
            watch.append(txt)
    # 資料缺口（fail-open 但誠實揭露）
    if sig.get("revenue_check") == "data_missing":
        watch.append("月營收資料本次無法取得，未經本業衰退檢查")
    if sig.get("quality_check") == "unavailable":
        watch.append("獲利品質資料本次無法取得，未經檢查")
    if sig.get("news_check") == "unavailable":
        watch.append("重大訊息公告本次無法取得，未經檢查")
    # 做空專屬風險
    if sig.get("direction") == "sell":
        if sig.get("executable") is False:
            watch.append("研究版放空訊號：門檻尚未經真實成交回測，且缺借券／軋空資料，勿直接執行")
        else:
            watch.append("放空特有風險：系統無借券可得性／費率／強制回補／軋空資料，下單前請自行確認券源")

    # ── 風險／報酬一句話 ──
    risk_bits = []
    rt = _num(sig.get("risk_twd"))
    rp = _num(sig.get("risk_pct"))
    if rt is not None:
        risk_bits.append(f"單筆風險 TWD {int(rt):,}" + (f"（本金 {rp:.1f}%）" if rp is not None else ""))
    rr1 = sig.get("rr1")
    if rr1 is not None:
        risk_bits.append(f"風報比 1:{rr1}")
    sl_pct = _num(sig.get("sl_pct"))
    tp1_pct = _num(sig.get("tp1_pct"))
    if sl_pct is not None and tp1_pct is not None:
        risk_bits.append(f"停損 {sl_pct:.1f}%／首目標 {tp1_pct:.1f}%")
    # ★ 2026-11-06 單位稽核修正：DB signals 表的欄位名為 suggested_lots，但實際存的是「股數」
    # （歷史遺留命名，見 state_store 與 scanner/app 多處註解）。記憶體訊號 dict 的 suggested_lots
    # 才是「張數」、另有 suggested_shares。本函式會被 /api/explain/signal 以 DB 列餵入，若把
    # suggested_lots 直接當張數顯示會 1000× 放大（2500 股→「2500 張」）。這裡一律以「股數」為準：
    # 優先取 suggested_shares（記憶體來源），否則把 suggested_lots 當股數（DB 來源），並換算張數。
    _sh = sig.get("suggested_shares")
    if _sh is None:
        _sh = sig.get("suggested_lots")
    _shn = _num(_sh)
    if _shn is not None and _shn >= 1:
        sh_i = int(_shn)
        risk_bits.append(f"建議 {sh_i:,} 股（約 {sh_i / 1000:.1f} 張）")
    risk = "｜".join(risk_bits) if risk_bits else "暫無"

    return {
        "ok": True,
        "code": sig.get("code"), "name": sig.get("name"),
        "direction": sig.get("direction"), "grade": grade, "score": score,
        "headline": headline,
        "why": why or ["此訊號的細部支持因子暫無紀錄"],
        "watch": watch,
        "risk": risk,
        "brief": (sig.get("reason_brief") or "").strip(),
        "disclaimer": _DISCLAIMER,
    }


def _posture(score: Optional[float], status: str, fn: float) -> Dict:
    """明日操作傾向（與 telegram_bot._generate_tomorrow_plan 同一套判斷邏輯，保持一致）。"""
    s = 50 if score is None else score
    if s >= 65 and fn >= 0 and status == "normal":
        return {"direction": "偏多操作", "strategy": "可積極建立多頭部位，量能放大才追"}
    if s >= 50:
        return {"direction": "中性觀察", "strategy": "選強勢股分批進場，嚴格執行停損"}
    return {"direction": "偏空謹慎", "strategy": "以觀望為主，避免逆勢操作"}


def daily_summary_text(signals: List[Dict], market_overview: Dict, scan_stats: Dict) -> Dict:
    """把當日掃描結果翻成白話摘要。市場數據缺則標暫無，不臆測。"""
    signals = signals or []
    market_overview = market_overview or {}
    scan_stats = scan_stats or {}
    now = datetime.now(_TPE)

    buy = [s for s in signals if s.get("direction") == "buy"]
    sell = [s for s in signals if s.get("direction") == "sell"]
    scanned = scan_stats.get("scanned")
    scanned_str = f"{scanned} 檔" if scanned else "暫無"

    headline = (f"今日掃描 {scanned_str}，產生 {len(signals)} 個訊號"
                f"（做多 {len(buy)}、做空 {len(sell)}）")

    # ── 市場環境（correct-or-暫無）──
    idx = market_overview.get("index", {}) or {}
    twii = idx.get("twii", {}) or {}
    vix = idx.get("vix", {}) or {}
    foreign = market_overview.get("foreign", {}) or {}
    score = market_overview.get("sentiment_score")
    status = market_overview.get("market_status", "normal")

    mkt_bits = []
    tchg = _num(twii.get("chg"))
    if twii.get("source") == "error" or not twii:
        mkt_bits.append("加權指數：暫無")
    elif tchg is not None:
        mkt_bits.append(f"加權指數 {'▲' if tchg >= 0 else '▼'}{abs(tchg):.2f}%")
    vprice = _num(vix.get("price"))
    if vix.get("source") == "error":
        mkt_bits.append("VIX：暫無")
    elif vprice is not None:
        mkt_bits.append(f"VIX {vprice:.1f}")
    fn = _num(foreign.get("net_buy_twd"))
    if fn is not None:
        mkt_bits.append(f"外資{'買超' if fn >= 0 else '賣超'} {abs(fn)/1e8:.1f} 億")
    if score is not None:
        mood = ("強烈看多" if score >= 80 else "偏多" if score >= 60 else
                "中性" if score >= 40 else "偏空" if score >= 20 else "強烈看空")
        mkt_bits.append(f"市場情緒{mood}（{score}/100）")
    market = "｜".join(mkt_bits) if mkt_bits else "暫無"

    # ── 重點（等級分布、最強前三、方向傾向）──
    points: List[str] = []
    if signals:
        ga = sum(1 for s in signals if s.get("grade") == "A")
        gb = sum(1 for s in signals if s.get("grade") == "B")
        gc = len(signals) - ga - gb
        points.append(f"等級分布：A 級 {ga}、B 級 {gb}、C 級 {gc}")
        top = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)[:3]
        for s in top:
            d = "做多" if s.get("direction") == "buy" else "做空"
            points.append(f"{s.get('name')}（{s.get('code')}）{d} {s.get('score')} 分"
                          + (f"：{(s.get('reason_brief') or '').strip()}" if s.get("reason_brief") else ""))
        if buy and not sell:
            points.append("今日清一色做多訊號，無做空機會")
        elif sell and not buy:
            points.append("今日清一色做空訊號，無做多機會")
    else:
        points.append("今日無符合條件的新訊號，維持觀望")

    # ── 風險提示（熔斷判斷不完整時誠實揭露）──
    caveats: List[str] = []
    if vix.get("source") == "error" and status == "normal":
        caveats.append("VIX 缺失，市場風險環境判斷不完整，綠燈僅供參考")
    if status == "data_error":
        caveats.append("大盤指數資料異常，今日不宜新倉")

    pos = _posture(score, status, fn or 0)

    return {
        "ok": True,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "headline": headline,
        "market": market,
        "points": points,
        "posture": f"明日方向：{pos['direction']} — {pos['strategy']}",
        "caveats": caveats,
        "counts": {"signals": len(signals), "buy": len(buy), "sell": len(sell),
                   "scanned": scanned},
        "disclaimer": _DISCLAIMER,
    }
