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
        # 逐期、逐檔補寫：公告期內同一期會陸續有公司公布，不能因為「這期已存過」就跳過
        by_period = {}
        for code, v in rev_map.items():
            p = str(v.get("period") or "")
            if len(p) < 5 or v.get("yoy_pct") is None:
                continue
            roc, mm = int(p[:-2]), p[-2:]
            period = f"{roc + 1911}-{mm}"
            by_period.setdefault(period, []).append({"ticker": code, "period": period, "yoy_pct": v.get("yoy_pct"),
                         "mom_pct": v.get("mom_pct"), "market": v.get("source", "")})
        for period, rows in by_period.items():
            key = f"rev_hist_n_{period}"
            if len(rows) > int(store.get_meta(key, 0) or 0):
                store.upsert_monthly_revenue_history(rows)
                store.set_meta(key, len(rows))
                logger.info(f"月營收歷史：{period} 寫入 {len(rows)} 筆")
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


def build_fundamental_profile(code: str, deep: bool = False) -> Dict:
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
    if deep:
        try:
            out["quality"] = build_quality(code)
        except Exception as e:
            out["quality"] = {"ok": False, "error": type(e).__name__}
    return out


def fund_adjustment(profile: Dict, direction: str) -> int:
    """基本面對技術訊號分數的調整（做多）。分數太低等於技術面再強也不碰。"""
    s = profile.get("score")
    if s is None or direction != "buy":
        return 0
    return 5 if s >= 80 else 3 if s >= 65 else 1 if s >= 50 else -3 if s >= 35 else -7 if s >= 20 else -12


def _mrows(ind: Dict, key: str) -> List[Dict]:
    for m in (ind or {}).get("metrics", []):
        if m["key"] == key:
            return m["rows"]
    return []


def build_quality(code: str) -> Dict:
    """財務體質體檢（只給個股研究頁用，資料全來自 FinMind 財報原始科目；掃描全市場時不呼叫，避免耗盡額度）。
    每項給 good/warn/bad，分數＝(good×1 ＋ warn×0.5) ÷ 有資料的項目數；缺資料的項目不算、不推估。"""
    import finmind_data as fm
    ind = fm.sec_indicators(code)
    if not ind.get("ok"):
        return {"ok": False, "error": ind.get("error") or "無財報資料"}
    industry = ind.get("industry") or ""
    fin = "金融" in industry or "保險" in industry          # 金融保險業：財報結構不同，毛利、流動比率、負債比率、現金流指標不適用
    cons = "營造" in industry                                # 建材營造：預售屋合約負債使負債比率天生偏高
    notes: List[str] = []
    if fin:
        notes.append("金融保險業的財報結構與一般公司不同，毛利率、營業利益率、負債比率、流動比率、現金流相關項目不適用，已不列入評分。")
    if cons:
        notes.append("建材營造業有預售屋合約負債，負債比率天生偏高，本檢查改用較寬鬆門檻（≤55% 優、≤70% 普通）。")
    items: List[Dict] = []

    def add(group, label, value, status, note):
        items.append({"group": group, "label": label, "value": value, "status": status, "note": note})

    def band(v, good, warn, higher=True):
        if higher:
            return "good" if v >= good else "warn" if v >= warn else "bad"
        return "good" if v <= good else "warn" if v <= warn else "bad"

    roe = _mrows(ind, "roe")
    if len(roe) >= 4:
        v = sum(r["value"] for r in roe[-4:])
        add("獲利能力", "ROE（近四季合計）", f"{v:.1f}%", band(v, 15, 8), "近四季單季 ROE 相加，≥15% 優、8~15% 普通、<8% 偏弱")
    gm = _mrows(ind, "gross")
    if not fin and gm and gm[-1].get("yoy_pp") is not None:
        r = gm[-1]
        add("獲利能力", f"毛利率 {r['period']}", f"{r['value']:.1f}%（同比 {r['yoy_pp']:+.1f}pt）",
            band(r["yoy_pp"], 0, -2), "與去年同季比：持平或上升為佳，下滑超過 2 個百分點偏弱")
    om = _mrows(ind, "op")
    if om and not fin:
        r = om[-1]
        add("獲利能力", f"營業利益率 {r['period']}", f"{r['value']:.1f}%",
            "bad" if r["value"] <= 0 else band(r["value"], 10, 3), "本業賺錢能力，≥10% 優、虧損為差")
    eps = _mrows(ind, "eps")
    if eps and eps[-1].get("yoy") is not None:
        r = eps[-1]
        add("成長動能", f"單季 EPS 同比 {r['period']}", f"{r['value']:.2f} 元（{r['yoy']:+.1f}%）",
            band(r["yoy"], 10, 0), "與去年同季相比")
    streak = 0
    for r in reversed(eps):
        if r.get("yoy") is not None and r["yoy"] > 0:
            streak += 1
        else:
            break
    if eps and any(r.get("yoy") is not None for r in eps):
        add("成長動能", "EPS 連續同比成長", f"{streak} 季", "good" if streak >= 4 else "warn" if streak >= 2 else "bad",
            "連續幾季 EPS 高於去年同季")
    rv = _mrows(ind, "revenue")
    if rv and rv[-1].get("yoy") is not None:
        r = rv[-1]
        add("成長動能", f"單季營收同比 {r['period']}", f"{r['value']:.1f} 億（{r['yoy']:+.1f}%）", band(r["yoy"], 10, 0), "與去年同季相比")
    dr = _mrows(ind, "debt_ratio")
    if dr and not fin:
        v = dr[-1]["value"]
        lo, hi = (55, 70) if cons else (40, 60)
        add("財務結構", "負債比率", f"{v:.1f}%", band(v, lo, hi, higher=False), f"總負債 ÷ 總資產，≤{lo}% 穩健、>{hi}% 偏高")
    cr = _mrows(ind, "current_ratio")
    if cr and not fin:
        v = cr[-1]["value"]
        add("財務結構", "流動比率", f"{v:.0f}%", band(v, 200, 120), "流動資產 ÷ 流動負債，≥200% 短期償債無虞")
    ocf, ni = _mrows(ind, "ocf"), _mrows(ind, "net_income")
    if not fin and len(ocf) >= 4 and len(ni) >= 4:
        o = sum(r["value"] for r in ocf[-4:]); n = sum(r["value"] for r in ni[-4:])
        if n > 0:
            add("現金流品質", "營業現金流 ÷ 淨利（近四季）", f"{o / n * 100:.0f}%", band(o / n * 100, 100, 60),
                "≥100% 代表賺的是真金白銀；偏低要留意應收帳款或存貨堆積")
    fc = _mrows(ind, "fcf")
    if not fin and len(fc) >= 4:
        v = sum(r["value"] for r in fc[-4:])
        add("現金流品質", "自由現金流（近四季合計）", f"{v:,.1f} 億", "good" if v > 0 else "bad", "營業現金流扣掉資本支出後，公司真正可自由運用的錢")
    try:
        dv = fm.sec_dividend(code)
        recs = dv.get("records") or []
        if dv.get("ok") and recs:
            yrs = sorted({r["period"] for r in recs if (r.get("cash") or 0) + (r.get("stock") or 0) > 0})
            last = recs[-1]
            add("股利", "近年配息", f"{len(yrs)}/{len({r['period'] for r in recs})} 年有配息；最近現金股利 {last['cash']:.2f} 元",
                "good" if recs and len(yrs) == len({r['period'] for r in recs}) and len(yrs) >= 3 else "warn" if yrs else "bad",
                "近幾年是否年年配息")
    except Exception:
        pass
    n = len(items)
    score = round(sum(1 if i["status"] == "good" else 0.5 if i["status"] == "warn" else 0 for i in items) / n * 100) if n else None
    return {"ok": n > 0, "score": score, "n": n, "items": items, "notes": notes, "industry": industry or None,
            "date": ind.get("date"), "stale": ind.get("stale"),
            "grade": None if score is None else "優" if score >= 75 else "中" if score >= 50 else "弱"}
