"""基本面評分：把月營收（單月／累計／加速度）、獲利品質（毛利率／營益率趨勢）、
估值（本益比／股價淨值比／殖利率）整合成一個 0-100 的基本面分數，
讓波段訊號不只看 K 線。缺資料的面向不計入分母，並明確回報資料涵蓋度。"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _persist_current_period(rev_map: Dict) -> None:
    """每個月第一次看到新一期全市場月營收時，順手存進資料庫，
    讓「連續幾個月成長／是否加速」的歷史自己越累積越完整，不必另外回填。"""
    try:
        from state_store import store
        periods = {v.get("period") for v in rev_map.values() if v.get("period")}
        if not periods:
            return
        covered = set(store.get_monthly_revenue_periods_covered())
        todo = []
        for code, v in rev_map.items():
            p = str(v.get("period") or "")
            if len(p) < 5 or v.get("yoy_pct") is None:
                continue
            roc, mm = int(p[:-2]), p[-2:]
            period = f"{roc + 1911}-{mm}"
            if period in covered:
                continue
            todo.append({"ticker": code, "period": period, "yoy_pct": v.get("yoy_pct"),
                         "mom_pct": v.get("mom_pct"), "market": v.get("source", "")})
        if todo:
            store.upsert_monthly_revenue_history(todo)
            logger.info(f"月營收歷史：新增 {len(todo)} 筆（期別 {sorted({t['period'] for t in todo})}）")
    except Exception as e:
        logger.warning(f"_persist_current_period: {e}")


def _growth(rev: Optional[Dict], series: List[Dict]):
    """成長面（滿分 50）：單月年增 25、累計年增 15、加速度／連續成長 10。"""
    if not rev or rev.get("yoy_pct") is None:
        return None
    tags, got, mx = [], 0, 0
    yoy, cum, mom = rev["yoy_pct"], rev.get("cum_yoy_pct"), rev.get("mom_pct")
    mx += 25
    for lim, pts in ((40, 25), (25, 21), (12, 16), (3, 11), (0, 7), (-10, 3)):
        if yoy >= lim:
            got += pts
            break
    if yoy >= 25: tags.append(f"月營收年增 {yoy:+.0f}%")
    elif yoy < 0: tags.append(f"月營收年減 {yoy:+.0f}%")
    if cum is not None:
        mx += 15
        for lim, pts in ((30, 15), (15, 12), (5, 9), (0, 6), (-8, 2)):
            if cum >= lim:
                got += pts
                break
        if cum >= 15: tags.append(f"累計營收年增 {cum:+.0f}%")
        elif cum < 0: tags.append(f"累計營收年減 {cum:+.0f}%")
    # 加速度：最新一期年增 vs 前幾期平均
    prev = [s["yoy_pct"] for s in series[1:4] if s.get("yoy_pct") is not None]
    accel = None
    if prev:
        accel = yoy - sum(prev) / len(prev)
    streak = 0
    for s in series:
        if s.get("yoy_pct") is not None and s["yoy_pct"] > 0:
            streak += 1
        else:
            break
    mx += 10
    if accel is not None:
        if accel >= 10 and yoy > 0: got += 6; tags.append(f"年增較近三月均值加速 {accel:+.0f}pt")
        elif accel >= 0 and yoy > 0: got += 4
        elif accel <= -15: tags.append(f"年增較近三月均值減速 {accel:+.0f}pt")
        if streak >= 3: got += 4; tags.append(f"連續 {streak} 個月營收年增為正")
        elif streak >= 2: got += 2
    else:  # 沒有歷史，只能用月增佐證
        if mom is not None and mom > 0 and yoy > 0: got += 5
        elif yoy > 0: got += 3
    return {"got": got, "max": mx, "tags": tags, "yoy": yoy, "cum_yoy": cum, "mom": mom,
            "accel": None if accel is None else round(accel, 1), "streak": streak}


def _profit(q: Optional[Dict]):
    """獲利面（滿分 30）：營益率水準 14、毛利率趨勢 8、營益率趨勢 8。"""
    if not q:
        return None
    om = q.get("operating_margin_pct_single_q")
    if om is None: om = q.get("operating_margin_pct")
    gm_chg, om_chg = q.get("gross_margin_chg"), q.get("operating_margin_chg")
    tags, got, mx = [], 0, 0
    if om is not None:
        mx += 14
        for lim, pts in ((20, 14), (12, 11), (6, 8), (2, 5), (0, 2)):
            if om >= lim:
                got += pts
                break
        if om < 0: tags.append(f"營業利益率 {om:.1f}%（虧損）")
        elif om >= 12: tags.append(f"營業利益率 {om:.1f}%")
    for chg, label in ((gm_chg, "毛利率"), (om_chg, "營益率")):
        if chg is None:
            continue
        mx += 8
        if chg >= 2: got += 8; tags.append(f"{label}季增 {chg:+.1f}pt")
        elif chg >= 0: got += 5
        elif chg >= -2: got += 3
        else: tags.append(f"{label}季減 {chg:+.1f}pt")
    if not mx:
        return None
    return {"got": got, "max": mx, "tags": tags, "op_margin": om, "gm_chg": gm_chg, "om_chg": om_chg,
            "gross_margin": q.get("gross_margin_pct_single_q", q.get("gross_margin_pct"))}


def _valuation(v: Optional[Dict]):
    """估值面（滿分 20）：本益比 12、殖利率 5、股價淨值比 3。虧損（無本益比）給 0。"""
    if not v:
        return None
    pe, pb, y = v.get("pe"), v.get("pb"), v.get("yield_pct")
    tags, got, mx = [], 0, 0
    try:
        pe = None if pe in (None, "", "-") else float(pe)
        pb = None if pb in (None, "", "-") else float(pb)
        y = None if y in (None, "", "-") else float(y)
    except (TypeError, ValueError):
        return None
    if pe is None and pb is None and y is None:
        return None
    mx += 12
    if pe is not None and pe > 0:
        for lim, pts in ((12, 12), (18, 10), (25, 7), (35, 4), (60, 2)):
            if pe <= lim:
                got += pts
                break
        tags.append(f"本益比 {pe:.1f}")
        if pe > 60: tags.append("本益比偏高")
    else:
        tags.append("本益比無（近四季虧損）")
    if y is not None:
        mx += 5
        got += 5 if y >= 5 else 4 if y >= 3.5 else 2 if y >= 2 else 0
        if y >= 4: tags.append(f"殖利率 {y:.1f}%")
    if pb is not None and pb > 0:
        mx += 3
        got += 3 if pb <= 1.5 else 2 if pb <= 3 else 1 if pb <= 5 else 0
    return {"got": got, "max": mx, "tags": tags, "pe": pe, "pb": pb, "yield": y}


def build_fundamental_profile(code: str) -> Dict:
    from fundamentals import (fetch_monthly_revenue_map, fetch_valuation_map, get_profitability_quality)
    from state_store import store
    out = {"code": code, "score": None, "grade": None, "coverage": 0, "parts": {}, "tags": [], "missing": []}
    rev_map = fetch_monthly_revenue_map()
    rev = rev_map.get(code)
    try:
        series = store.get_revenue_series(code, 8)
    except Exception:
        series = []
    try:
        q = get_profitability_quality(code)
    except Exception:
        q = None
    try:
        val = fetch_valuation_map().get(code)
    except Exception:
        val = None
    parts = {"成長": (_growth(rev, series), 50), "獲利": (_profit(q), 30), "估值": (_valuation(val), 20)}
    got = mx = full = 0
    for k, (p, cap) in parts.items():
        full += cap
        if p is None:
            out["missing"].append(k)
            continue
        got += p["got"]; mx += p["max"]
        out["parts"][k] = {"score": round(p["got"] / p["max"] * 100) if p["max"] else 0,
                           "weight": cap, **{kk: vv for kk, vv in p.items() if kk not in ("got", "max")}}
        out["tags"] += p["tags"]
    out["coverage"] = round(mx / full * 100) if full else 0
    if mx and out["coverage"] >= 40:
        out["score"] = round(got / mx * 100)
        s = out["score"]
        out["grade"] = "強" if s >= 70 else "中" if s >= 50 else "弱" if s >= 30 else "差"
    if rev:
        out["revenue"] = {k: rev.get(k) for k in ("period", "revenue", "revenue_ly", "cum_revenue", "yoy_pct", "mom_pct", "cum_yoy_pct")}
        out["name"] = rev.get("name")
    out["history"] = list(reversed(series))
    return out


def fund_adjustment(profile: Dict, direction: str) -> int:
    """基本面對技術訊號分數的調整（做多）。分數太低等於技術面再強也不碰。"""
    s = profile.get("score")
    if s is None or direction != "buy":
        return 0
    return 5 if s >= 80 else 3 if s >= 65 else 1 if s >= 50 else -3 if s >= 35 else -7 if s >= 20 else -12
