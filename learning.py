"""
策略學習報告：從 shadow_signals 的已結算資料，用誠實的統計方式回答「哪些條件有效」。

規則：
- 勝＝觸及任一停利（tp1/tp2/tp3）；敗＝停損；expired（逾期未到價）另計，但仍計入平均 R。
- 主要看「平均 R（期望值）」，勝率只是輔助——因為停損與停利的距離不同，勝率高不代表賺錢。
- 樣本 < 30 的分組只顯示、不下結論。
- 顯著性以「交易日為叢集的拔靴（bootstrap）」計算平均 R 的差異與 95% 信賴區間——
  因為同一天的多檔訊號高度相關，把每筆當成獨立會低估變異、灌大統計量。
- 多重比較防護：同時檢驗幾十個分組，純靠運氣也會有幾個「達標」。所以
    * 累積不到 10 個交易日，一律不列結論；
    * 單組拔靴 p < 0.05 只標示「初步（需新樣本再驗證）」；
    * 全部分組一起用 Benjamini-Hochberg 控制偽發現率（q=0.10）且前後兩段時間方向一致，才標示「較可靠」。
- 對照組（沒有訊號、隨機抽樣、同一套停損停利機制）回答「訊號有沒有比隨便買好」。
- 被規則擋下的假想單（kind=rejected）回答「這條規則是幫了我、還是擋掉了賺錢的單」。
"""
import json, math, random, logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

MIN_N = 30
MIN_DAYS = 10
Z_TENT, Z_STRONG = 1.96, 3.2
WIN = ("tp1", "tp2", "tp3")
N_BOOT = 1000       # 拔靴重抽次數
FDR_Q = 0.10        # Benjamini-Hochberg 偽發現率門檻（標示「較可靠」）
BOOT_SEED = 20261009  # 固定種子，使同一批資料的報告結果可重現、不會每次刷新就跳動


def _rows():
    from state_store import store
    import shadow
    shadow.ensure_table()
    with store._conn() as conn:
        rs = [dict(r) for r in conn.execute("SELECT * FROM shadow_signals ORDER BY bar_date").fetchall()]
    for r in rs:
        try:
            r["f"] = json.loads(r.get("features") or "{}")
        except Exception:
            r["f"] = {}
    return rs


def _rl(rows):
    return [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]


def _mean_sd(xs):
    n = len(xs)
    if not n:
        return 0.0, 0.0
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return m, sd


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    w = sum(1 for r in rows if r["result"] in WIN)
    l = sum(1 for r in rows if r["result"] == "sl")
    e = n - w - l
    rs = _rl(rows)
    m, sd = _mean_sd(rs)
    r10 = [r["r10"] for r in rows if r.get("r10") is not None]
    mf = [r["mfe_r"] for r in rows if r.get("mfe_r") is not None]
    ma = [r["mae_r"] for r in rows if r.get("mae_r") is not None]
    bh = [r["bars_held"] for r in rows if r.get("bars_held") is not None]
    t = round(m / (sd / math.sqrt(len(rs))), 2) if len(rs) > 1 and sd > 0 else None
    # 出場原因分布：了解停利目標是否務實（多半只到 tp1？還是常直接停損？）
    exits = {}
    for r in rows:
        res = r.get("result")
        if res:
            exits[res] = exits.get(res, 0) + 1
    # ── 2026-10-16 更詳細的績效指標（皆由已結算的 R 計算）──
    wins_r = [r["r_multiple"] for r in rows if r.get("result") in WIN and r.get("r_multiple") is not None]
    loss_r = [r["r_multiple"] for r in rows if r.get("result") == "sl" and r.get("r_multiple") is not None]
    gross_win = sum(x for x in rs if x > 0)
    gross_loss = -sum(x for x in rs if x < 0)
    avg_win = sum(wins_r) / len(wins_r) if wins_r else None
    avg_loss = sum(loss_r) / len(loss_r) if loss_r else None
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 1e-9 else (None if gross_win == 0 else 99.9)
    payoff = round(abs(avg_win / avg_loss), 2) if (avg_win is not None and avg_loss not in (None, 0)) else None
    sharpe = round(m / sd, 2) if sd > 0 else None        # 每筆 R 的夏普式比值（期望值÷波動）
    # 最長連敗（依日期序，停損或 R<0 視為敗）
    mcl = cur = 0
    for r in rows:
        rv = r.get("r_multiple")
        loss_flag = r.get("result") == "sl" or (rv is not None and rv < 0)
        cur = cur + 1 if loss_flag else 0
        mcl = max(mcl, cur)
    tbs = [r["tp1_before_sl"] for r in rows if r.get("tp1_before_sl") is not None]
    return {"n": n, "win": w, "loss": l, "expired": e,
            "win_rate": round(w / n * 100, 1), "stop_rate": round(l / n * 100, 1),
            "avg_r": round(m, 3) if rs else None, "t_vs0": t,
            "avg_r10": round(sum(r10) / len(r10), 3) if r10 else None, "n_r10": len(r10),
            "mfe": round(sum(mf) / len(mf), 2) if mf else None,
            "mae": round(sum(ma) / len(ma), 2) if ma else None,
            "avg_bars_held": round(sum(bh) / len(bh), 1) if bh else None,
            "exits": exits,
            "profit_factor": profit_factor, "payoff": payoff, "sharpe": sharpe,
            "avg_win_r": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss_r": round(avg_loss, 2) if avg_loss is not None else None,
            "best_r": round(max(rs), 2) if rs else None, "worst_r": round(min(rs), 2) if rs else None,
            "total_r": round(sum(rs), 1) if rs else None,
            "max_consec_loss": mcl,
            "tp1_before_sl_rate": round(sum(tbs) / len(tbs) * 100, 1) if tbs else None}


def _z(w1, n1, w2, n2):
    if n1 < 1 or n2 < 1:
        return 0.0
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    return (w1 / n1 - w2 / n2) / se


def _zr(a, b):
    """兩組平均 R 的 Welch z。"""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, sa = _mean_sd(a)
    mb, sb = _mean_sd(b)
    se = math.sqrt(sa ** 2 / len(a) + sb ** 2 / len(b)) or 1e-9
    return (ma - mb) / se


def _by_day(rows):
    """{bar_date: [r_multiple,...]}，只收已結算且有 R 的列。同一天多筆高度相關，拔靴以『日』為重抽單位。"""
    d = {}
    for r in rows:
        if r.get("r_multiple") is None:
            continue
        d.setdefault(r["bar_date"], []).append(r["r_multiple"])
    return d


def _boot_ci(rows, n_boot=N_BOOT, seed=BOOT_SEED):
    """以交易日為叢集對平均 R 做拔靴，回傳 (mean, lo95, hi95)。日數不足時回 (mean, None, None)。"""
    day = _by_day(rows); days = list(day)
    allv = [v for vs in day.values() for v in vs]
    if not allv:
        return (None, None, None)
    m = sum(allv) / len(allv)
    if len(days) < 2 or len(allv) < 2:
        return (round(m, 3), None, None)
    rng = random.Random(seed); means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(days)):
            pool.extend(day[days[rng.randrange(len(days))]])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return (round(m, 3), round(lo, 3), round(hi, 3))


def _boot_diff(a_rows, b_rows, n_boot=N_BOOT, seed=BOOT_SEED):
    """bucket(a) vs rest(b) 的平均 R 差，以交易日為叢集做兩樣本拔靴（同一天同時貢獻兩組，保留當日共同波動）。
    回傳 (diff, p_two_sided, pseudo_z)。樣本不足回 (None, 1.0, 0.0)。"""
    ad = _by_day(a_rows); bd = _by_day(b_rows)
    days = sorted(set(ad) | set(bd))
    ao = [v for vs in ad.values() for v in vs]; bo = [v for vs in bd.values() for v in vs]
    if len(ao) < 2 or len(bo) < 2 or len(days) < 2:
        return (None, 1.0, 0.0)
    obs = sum(ao) / len(ao) - sum(bo) / len(bo)
    rng = random.Random(seed); diffs = []
    for _ in range(n_boot):
        pa = []; pb = []
        for _ in range(len(days)):
            dd = days[rng.randrange(len(days))]
            pa.extend(ad.get(dd, [])); pb.extend(bd.get(dd, []))
        if pa and pb:
            diffs.append(sum(pa) / len(pa) - sum(pb) / len(pb))
    if len(diffs) < 2:
        return (round(obs, 3), 1.0, 0.0)
    # ★ 2026-11-11 SH10：蒙地卡羅 p 值採 (b+1)/(B+1) 加一校正，避免 p 顯示為 0（那只是重抽次數的
    # 解析度下界、非真正機率為零）；p 的下界 ≈ 2/(B+1)，誠實反映解析度。
    B = len(diffs)
    frac_le = (sum(1 for d in diffs if d <= 0) + 1) / (B + 1)
    frac_ge = (sum(1 for d in diffs if d >= 0) + 1) / (B + 1)
    p = min(1.0, 2 * min(frac_le, frac_ge))
    md = sum(diffs) / len(diffs)
    sd = math.sqrt(sum((d - md) ** 2 for d in diffs) / (len(diffs) - 1)) or 1e-9
    z = max(-99.0, min(99.0, obs / sd))      # 夾住：拔靴變異趨近 0 時避免 z 爆成天文數字
    return (round(obs, 3), round(p, 4), round(z, 2))


def _bh_reject(pvals, q=FDR_Q):
    """Benjamini-Hochberg：回傳一組布林，標示哪些 p 值在控制偽發現率 q 下為顯著。
    修正同時檢驗數十個分組時『純靠運氣也會有幾個達標』的多重比較問題。"""
    m = len(pvals)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    thresh_k = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= rank / m * q:
            thresh_k = rank
    keep = set(order[:thresh_k]) if thresh_k > 0 else set()
    return [i in keep for i in range(m)]


def _band(v, edges, labels):
    if v is None:
        return None
    for e, lb in zip(edges, labels):
        if v < e:
            return lb
    return labels[-1]


def _f(r, k):
    return r["f"].get(k)


def _dims():
    return [
        # ★ 新增：2026-10-16——使用者要求策略學習也要涵蓋做空，並能明確看到做空
        # 的表現。放在最前面，讓做多 vs 做空的勝率／平均 R 直接並列比較。
        ("方向", lambda r: {"buy": "做多", "sell": "做空"}.get(r.get("direction"))),
        ("分數", lambda r: _band(r.get("score"), [70, 75, 80, 85], ["65–69", "70–74", "75–79", "80–84", "85+"])),
        ("追高等級", lambda r: {"low": "低", "mid": "中", "high": "高"}.get(_f(r, "chase_level"))),
        ("基本面等級", lambda r: _f(r, "f_grade") or "未評分"),
        ("基本面分數", lambda r: _band(_f(r, "f_score"), [35, 50, 65, 80], ["<35", "35–49", "50–64", "65–79", "80+"])),
        ("營收年增", lambda r: _band(_f(r, "rev_yoy"), [0, 10, 25, 50], ["衰退", "0–10%", "10–25%", "25–50%", "50%+"])),
        ("ADX", lambda r: _band(_f(r, "adx_value"), [20, 25, 30, 40], ["<20", "20–25", "25–30", "30–40", "40+"])),
        ("RSI", lambda r: _band(_f(r, "rsi_value"), [40, 50, 60, 70], ["<40", "40–50", "50–60", "60–70", "70+"])),
        ("量比（多週期）", lambda r: _band(_f(r, "vol_ratio"), [1.2, 1.5, 2, 3], ["<1.2", "1.2–1.5", "1.5–2", "2–3", "3+"])),
        ("今日量／20 日均量", lambda r: _band(_f(r, "vol_x20"), [0.8, 1.2, 2, 3], ["<0.8", "0.8–1.2", "1.2–2", "2–3", "3+"])),
        ("停損距離(ATR)", lambda r: _band(_f(r, "sl_atr"), [1.5, 2, 2.5, 3], ["<1.5", "1.5–2", "2–2.5", "2.5–3", "3+"])),
        ("離均線延伸(ATR)", lambda r: _band(_f(r, "ext_atr"), [1, 2, 3], ["<1", "1–2", "2–3", "3+"])),
        ("波動 ATR%", lambda r: _band(_f(r, "atr_pct"), [2, 3, 4, 6], ["<2%", "2–3%", "3–4%", "4–6%", "6%+"])),
        ("近 20 日漲幅", lambda r: _band(_f(r, "ret20"), [-5, 0, 10, 20], ["<-5%", "-5–0%", "0–10%", "10–20%", "20%+"])),
        ("近 5 日漲幅", lambda r: _band(_f(r, "ret5"), [-3, 0, 5, 10], ["<-3%", "-3–0%", "0–5%", "5–10%", "10%+"])),
        ("離 52 週高點", lambda r: _band(_f(r, "dist_hi52"), [-30, -15, -5, -1], ["<-30%", "-30~-15%", "-15~-5%", "-5~-1%", "創高附近"])),
        ("當日跳空", lambda r: _band(_f(r, "gap_today"), [-1, 1, 3], ["跳空下", "平開", "跳空上1–3%", "跳空上3%+"])),
        ("週線", lambda r: _f(r, "weekly_bias")),
        ("規模", lambda r: r.get("size_cat")),
        ("大盤情緒", lambda r: _band(_f(r, "sentiment"), [40, 60], ["偏弱<40", "中性", "偏強≥60"])),
        ("星期", lambda r: "一二三四五六日"[int(r["f"]["dow"])] if _f(r, "dow") is not None else None),
        ("市場", lambda r: "上櫃" if (r.get("ticker") or "").endswith(".TWO") else "上市"),
        # ── 2026-10-15 新增分析維度（善用已搜集但先前未分析的特徵）──
        ("相對大盤強弱(20日)", lambda r: _band(_f(r, "rs20"), [-5, 0, 5, 15], ["弱<-5%", "-5~0%", "0~5%", "5~15%", "強15%+"])),
        ("相對產業強弱(20日)", lambda r: _band(_f(r, "sec_rel20"), [-5, 0, 5], ["落後<-5%", "-5~0%", "0~5%", "領先5%+"])),
        ("突破前20日高", lambda r: _band(_f(r, "brk20"), [-5, 0, 3], ["遠低於<-5%", "-5~0%", "剛突破0~3%", "突破3%+"])),
        ("20日線斜率", lambda r: _band(_f(r, "ma20_slope"), [-1, 0, 1, 3], ["下彎<-1%", "-1~0%", "0~1%", "1~3%", "陡升3%+"])),
        ("5日線斜率", lambda r: _band(_f(r, "ma5_slope"), [-2, 0, 2, 5], ["下彎<-2%", "-2~0%", "0~2%", "2~5%", "陡升5%+"])),
        ("連漲天數", lambda r: _band(_f(r, "up_days"), [0, 1, 3, 5], ["連跌", "0", "1~2", "3~4", "5+"])),
        ("收盤區間位置", lambda r: _band(_f(r, "clv"), [33, 66], ["收低檔<33%", "收中段", "收高檔≥66%"])),
        ("量能趨勢(5日)", lambda r: _band(_f(r, "vol_trend5"), [0.8, 1.2, 1.8], ["縮量<0.8", "0.8~1.2", "1.2~1.8", "爆量1.8+"])),
        ("站上20日線天數", lambda r: _band(_f(r, "above20_days"), [1, 5, 20], ["跌破", "1~4", "5~19", "20+"])),
        ("大盤型態", lambda r: _f(r, "regime")),
        ("近期負面消息", lambda r: {0: "無", 1: "有"}.get(_f(r, "news_neg"))),
        # ── 2026-10-16 新增維度（善用本次擴充搜集的特徵）──
        ("52週區間位置", lambda r: _band(_f(r, "pos52"), [25, 50, 75, 90], ["底部<25%", "25~50%", "50~75%", "75~90%", "高檔90%+"])),
        ("均線多頭排列", lambda r: {0: "全空", 1: "弱", 2: "中", 3: "完全多頭"}.get(_f(r, "ma_stack"))),
        ("近20日最大回檔", lambda r: _band(_f(r, "maxdd20"), [-15, -10, -5], ["深<-15%", "-15~-10%", "-10~-5%", "淺>-5%"])),
        ("年化波動(60日)", lambda r: _band(_f(r, "vol60_ann"), [25, 40, 60], ["低<25%", "25~40%", "40~60%", "高60%+"])),
        ("今日量能 z 分數", lambda r: _band(_f(r, "vol_z20"), [0, 1, 2], ["低於均量", "0~1", "1~2", "爆量2σ+"])),
        ("今日 K 棒實體", lambda r: _band(_f(r, "body_pct"), [25, 50, 75], ["十字<25%", "25~50%", "50~75%", "長實體75%+"])),
        ("月份", lambda r: f"{int(r['f']['month'])}月" if _f(r, "month") is not None else None),
    ]


def _readable_reason(code):
    try:
        import shadow
        return shadow.REASONS.get(code, code)
    except Exception:
        return code


# ─────────── 分數 ↔ 淨 R 校準（2026-11-06，P1）───────────
# 規格 P1/CSV 觀察：「分數提高未呈現更佳 net R，相關性弱且略負，高分不等於高品質」。
# 這裡用『已結算候選』的規則分數與實際淨 R，算 Spearman 等級相關＋分桶平均淨 R，直接回答
# 「分數有沒有換到更好的淨 R、是否單調」。純研究、不改評分；多空分開；排除退化(bad_risk)列。
def _rank_avg(xs):
    """平均名次（處理同分並列），1 起算。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i
        while j + 1 < len(xs) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs, ys):
    """Spearman 等級相關 = 名次上的 Pearson。樣本<3 或零變異回 None。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _rank_avg(xs), _rank_avg(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _score_verdict(rho, mono):
    if rho is None:
        return "樣本不足，無法判定分數與淨 R 的關係"
    a = abs(rho)
    strength = "幾乎無" if a < 0.1 else "偏弱" if a < 0.3 else "中等" if a < 0.5 else "偏強"
    sign = "正" if rho > 0 else "負"
    tail = f"；{mono}" if mono else ""
    return f"分數與淨 R 為{sign}相關（Spearman={rho:.2f}，{strength}）{tail}"


def _calib_one(side_rows, label):
    n = len(side_rows)
    if n < 20:
        return {"side": label, "n": n, "insufficient": True,
                "note": "已結算樣本不足 20 筆，暫不判定（樣本累積中）"}
    scores = [float(r["score"]) for r in side_rows]
    rr = [float(r["r_multiple"]) for r in side_rows]
    rho = _spearman(scores, rr)
    edges, labels = [70, 75, 80, 85], ["65–69", "70–74", "75–79", "80–84", "85+"]
    buckets = {}
    for r in side_rows:
        buckets.setdefault(_band(r.get("score"), edges, labels), []).append(r)
    brows = []
    for lb in labels:
        xs = buckets.get(lb, [])
        if not xs:
            brows.append({"bucket": lb, "n": 0})
            continue
        m, lo, hi = _boot_ci(xs)
        wr = sum(1 for r in xs if r["result"] in WIN) / len(xs) * 100
        brows.append({"bucket": lb, "n": len(xs), "avg_net_r": round(m, 3),
                      "ci_lo": None if lo is None else round(lo, 3),
                      "ci_hi": None if hi is None else round(hi, 3),
                      "win_rate": round(wr, 1)})
    seq = [b["avg_net_r"] for b in brows if b.get("n", 0) >= 10 and "avg_net_r" in b]
    mono = None
    if len(seq) >= 3:
        inc = all(seq[i] <= seq[i + 1] + 1e-9 for i in range(len(seq) - 1))
        mono = "分桶平均淨 R 隨分數單調上升" if inc else "非單調：分數更高不保證淨 R 更好"
    return {"side": label, "n": n, "spearman": None if rho is None else round(rho, 3),
            "buckets": brows, "monotonic": mono, "verdict": _score_verdict(rho, mono)}


def score_calibration():
    """分數↔淨R 校準主入口。只用已結算、有淨R、非退化(bad_risk)的候選。多空分開。"""
    try:
        rows = [r for r in _rows()
                if r.get("kind") == "candidate" and r.get("status") == "closed"
                and r.get("r_multiple") is not None and r.get("result") != "bad_risk"
                and r.get("score") is not None]
        buy = [r for r in rows if r.get("direction") == "buy"]
        sell = [r for r in rows if r.get("direction") == "sell"]
        return {"ok": True,
                "overall": _calib_one(rows, "全部"),
                "buy": _calib_one(buy, "做多"),
                "sell": _calib_one(sell, "做空"),
                "disclaimer": "樣本期間短、同日市場共振、ticker 重複，非獨立大樣本；僅供觀察，非投資建議。"}
    except Exception as e:
        logger.warning(f"score_calibration: {e}")
        return {"ok": False, "error": str(e)}


# ─────────── 因子增量驗證：Rank IC / ICIR（2026-11-08，P1-3）───────────
# 規格 §5.3：量化每個因子對未來報酬的預測力，分辨「真有增量 alpha」vs「只是重複計分」。
# 作法：對每個數值特徵，算它與實際淨 R 的 Rank IC（Spearman），附樣本數、近似 p 值與 BH-FDR
# 顯著性，依 |IC| 排序。純研究、不改評分；誠實揭露：此為「合併樣本」Rank IC（非每日橫斷面），
# 樣本期間短、同日/同股相關，僅供觀察。
_IC_MIN_N = 30              # 少於此樣本數的因子不給 IC（避免雜訊充當訊號）
_IC_SKIP = {"score", "month", "dow"}   # score 已有專屬校準；month/dow 為日曆特徵，非 alpha

# ★ 2026-11-11 SH09：識別／決策衍生欄。絕對價位、部位股數、成本與決策輸出由決策本身決定
# （非「當下可量測、用來預測未來報酬」的 alpha 因子）。它們與淨 R 的相關多半是價位水準／
# 部位規模的機械關聯，不能當作「這個因子有預測力」的證據。這裡**不隱藏、仍照算**，但標記
# derived=True，前端與技術／基本面因子分開呈現（審查 SH09：不一律與技術因子混排）。
_IC_DERIVED = {
    # 絕對價位（元）
    "current_price", "entry_price", "entry_zone_low", "entry_zone_high",
    "stop_loss", "tp1", "tp2", "tp3", "atr",
    # 決策參數／輸出（停損距離、停利距離、風報比、追高、分級、部位類別、到期窗）
    "sl_pct", "tp1_pct", "tp2_pct", "tp3_pct", "rr1", "rr2", "rr3",
    "sl_atr", "chase_pen", "chase_level", "grade", "size_cat", "expire_days",
    # 部位／成本
    "suggested_shares", "suggested_lots", "risk_twd", "risk_pct",
    "position_value", "roundtrip_cost", "breakeven_pct",
    # 識別／決策 meta
    "pnl_twd", "model_prob",
}


def _is_derived(key) -> bool:
    """該特徵是否為識別／決策衍生欄（非 alpha 預測因子）。"""
    return key in _IC_DERIVED


def _norm_sf(z):
    """標準常態右尾機率 P(Z>z)，用 math.erf 實作（免 scipy）。"""
    return 0.5 * math.erfc(z / math.sqrt(2))


def _quantile_returns(xs, ys, q=5):
    """依因子值排序分 q 組（低→高），回傳各組平均淨 R 與組內筆數，以及是否單調（遞增/遞減）。
    規格 §5.3「因子分位數未來報酬」：看因子由低到高，實際淨 R 是否單調改變（比單一 IC 更可讀）。"""
    pairs = sorted(zip(xs, ys), key=lambda t: t[0])
    n = len(pairs)
    if n < q * 4:                       # 每組至少 ~4 筆才分組，否則不給分位
        return None, None
    buckets = []
    for i in range(q):
        lo = n * i // q
        hi = n * (i + 1) // q
        seg = [p[1] for p in pairs[lo:hi]]
        buckets.append({"q": i + 1, "n": len(seg),
                        "avg_net_r": round(sum(seg) / len(seg), 3) if seg else None})
    avgs = [b["avg_net_r"] for b in buckets if b["avg_net_r"] is not None]
    mono = None
    if len(avgs) == q:
        inc = all(avgs[i] <= avgs[i + 1] + 1e-9 for i in range(q - 1))
        dec = all(avgs[i] >= avgs[i + 1] - 1e-9 for i in range(q - 1))
        mono = "遞增" if inc else "遞減" if dec else "非單調"
    return buckets, mono


def _ic_one(rows, label):
    n0 = len(rows)
    if n0 < _IC_MIN_N:
        return {"side": label, "n": n0, "insufficient": True,
                "note": "已結算樣本不足，暫不計算因子 IC（樣本累積中）"}
    # 收集每個數值特徵在各列的值
    keys = {}
    for r in rows:
        f = r.get("f") or {}
        for k, v in f.items():
            if k in _IC_SKIP:
                continue
            fv = _num_or_none(v)
            if fv is not None:
                keys.setdefault(k, 0)
                keys[k] += 1
    out = []
    pvals, idx = [], []
    for k in keys:
        xs, ys = [], []
        for r in rows:
            fv = _num_or_none((r.get("f") or {}).get(k))
            rm = _num_or_none(r.get("r_multiple"))
            if fv is not None and rm is not None:
                xs.append(fv); ys.append(rm)
        if len(xs) < _IC_MIN_N:
            continue
        ic = _spearman(xs, ys)
        if ic is None:
            continue
        n = len(xs)
        z = abs(ic) * math.sqrt(n - 1)            # Spearman 虛無下近似常態
        p = 2 * _norm_sf(z)
        q_buckets, q_mono = _quantile_returns(xs, ys)
        rec = {"factor": k, "ic": round(ic, 3), "n": n, "p": round(p, 4),
               "quantiles": q_buckets, "q_monotonic": q_mono,
               "derived": _is_derived(k)}            # ★ SH09：識別／決策衍生欄另類別
        out.append(rec); pvals.append(p); idx.append(len(out) - 1)
    # BH-FDR：在多因子下控制偽發現，標示「較可靠」
    if pvals:
        flags = _bh_reject(pvals, FDR_Q)
        for j, f in zip(idx, flags):
            out[j]["significant"] = bool(f)
    # alpha 因子優先（derived 排後），各自再依 |IC| 排序；上限 40 不讓衍生欄擠掉技術因子
    out.sort(key=lambda d: (d["derived"], -abs(d["ic"])))
    return {"side": label, "n": n0, "factors": out[:40]}


def _num_or_none(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e12 else None
    except (TypeError, ValueError):
        return None


# ─────────── 每日橫斷面 IC → ICIR（2026-11-09，§三 3.3 第一刀）───────────
# 合併樣本 IC 把所有日期的樣本混在一起算，受「同日/同股群聚」污染。標準作法是：每個交易日在
# 當日多檔之間算一次橫斷面 Rank IC，得到每日 IC 序列，再取 ICIR = mean(日IC)/std(日IC)——
# 衡量因子預測力的「穩定度」而非單次強度。純研究、不改評分；多空分開、不混用。
_ICIR_MIN_DAYS = 5          # 少於此天數不給 ICIR（序列太短、std 無意義）
_IC_DAY_MIN_BREADTH = 4     # 單日橫斷面至少 N 檔才算當日 IC（1~3 檔的 Spearman 無鑑別力）


def _daily_ic_one(rows, factor_key, ret_key):
    """單一因子的每日橫斷面 IC 序列與 ICIR。ret_key 可為 r_multiple（Research）或 champion_r（Champion）。"""
    by_day = {}
    for r in rows:
        fv = _num_or_none((r.get("f") or {}).get(factor_key))
        rv = _num_or_none(r.get(ret_key))
        if fv is None or rv is None:
            continue
        by_day.setdefault(r.get("bar_date"), []).append((fv, rv))
    day_ics = []
    for _d, pairs in by_day.items():
        if len(pairs) < _IC_DAY_MIN_BREADTH:
            continue
        ic = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            day_ics.append(ic)
    nd = len(day_ics)
    if nd < _ICIR_MIN_DAYS:
        return None
    mean_ic, sd_ic = _mean_sd(day_ics)
    icir = mean_ic / sd_ic if sd_ic > 1e-9 else None
    t = mean_ic / (sd_ic / math.sqrt(nd)) if sd_ic > 1e-9 else None   # 檢定 mean(日IC) 是否顯著 ≠ 0
    p = 2 * _norm_sf(abs(t)) if t is not None else None
    return {"factor": factor_key, "n_days": nd,
            "mean_ic": round(mean_ic, 3), "ic_std": round(sd_ic, 3),
            "icir": round(icir, 3) if icir is not None else None,
            "t_stat": round(t, 2) if t is not None else None,
            "p": round(p, 4) if p is not None else None,
            "pct_pos_days": round(sum(1 for x in day_ics if x > 0) / nd * 100)}


def _daily_ic_side(rows, ret_key, label):
    """某一方向（做多/做空）下，各因子的每日 ICIR，附 BH-FDR 顯著性，依 |ICIR| 排序。"""
    keys = set()
    for r in rows:
        for k, v in (r.get("f") or {}).items():
            if k not in _IC_SKIP and _num_or_none(v) is not None:
                keys.add(k)
    recs, pvals, idx = [], [], []
    for k in keys:
        rec = _daily_ic_one(rows, k, ret_key)
        if rec is None:
            continue
        rec["derived"] = _is_derived(k)              # ★ SH09：識別／決策衍生欄另類別
        recs.append(rec)
        if rec.get("p") is not None:
            pvals.append(rec["p"]); idx.append(len(recs) - 1)
    if pvals:
        flags = _bh_reject(pvals, FDR_Q)
        for j, fl in zip(idx, flags):
            recs[j]["significant"] = bool(fl)
    # alpha 因子優先（derived 排後），各自再依 |ICIR| 排序
    recs.sort(key=lambda d: (d["derived"], -(abs(d["icir"]) if d.get("icir") is not None else 0)))
    return {"side": label, "n_days_max": max((r["n_days"] for r in recs), default=0),
            "factors": recs[:40]}


def _daily_ic_block(buy, sell):
    """每日 ICIR 區塊：Research（r_multiple，單次出場）必給；Champion（champion_r，分批）有值才給。"""
    # ★ SH09：有效日期範圍與交易日數（breadth 的時間維度），讓讀者知道序列落在哪段期間、涵蓋幾個交易日
    _days = sorted({r.get("bar_date") for r in (buy + sell) if r.get("bar_date")})
    span = {"first_day": _days[0] if _days else None,
            "last_day": _days[-1] if _days else None,
            "n_days": len(_days)}
    block = {"research": {"buy": _daily_ic_side(buy, "r_multiple", "做多"),
                          "sell": _daily_ic_side(sell, "r_multiple", "做空")},
             "span": span,
             "metric": "每日橫斷面 Rank IC（Spearman）→ ICIR = mean(日IC)/std(日IC)",
             "params": {"min_days": _ICIR_MIN_DAYS, "min_breadth": _IC_DAY_MIN_BREADTH},
             "disclaimer": "每日橫斷面 IC：每個交易日在當日多檔間算因子 vs 淨 R 的 Rank IC，再取序列 "
                           "mean/std 得 ICIR（穩定度）。樣本期間短、單日檔數少時 ICIR 不穩，僅供觀察；"
                           "多空分開、不混用。Research=單次出場 r_multiple；Champion=分批出場 champion_r。"
                           "識別／決策衍生欄（絕對價位、部位股數、成本）另列、非 alpha 證據。"}
    has_champ = any(_num_or_none(r.get("champion_r")) is not None for r in buy) \
        or any(_num_or_none(r.get("champion_r")) is not None for r in sell)
    if has_champ:
        block["champion"] = {"buy": _daily_ic_side(buy, "champion_r", "做多"),
                             "sell": _daily_ic_side(sell, "champion_r", "做空")}
    return block


def factor_ic_report():
    """因子 Rank IC 主入口。只用已結算、有淨R、非退化的候選；多空分開。"""
    try:
        rows = [r for r in _rows()
                if r.get("kind") == "candidate" and r.get("status") == "closed"
                and r.get("r_multiple") is not None and r.get("result") != "bad_risk"]
        buy = [r for r in rows if r.get("direction") == "buy"]
        sell = [r for r in rows if r.get("direction") == "sell"]
        return {"ok": True,
                "overall": _ic_one(rows, "全部"),
                "buy": _ic_one(buy, "做多"),
                "sell": _ic_one(sell, "做空"),
                "daily": _daily_ic_block(buy, sell),       # ★ 2026-11-09 §三 3.3：每日橫斷面 ICIR
                "metric": "Rank IC（Spearman，因子值 vs 實際淨 R）",
                "disclaimer": "合併樣本 Rank IC（非每日橫斷面）；樣本期間短、同日/同股相關，"
                              "IC 僅供觀察、非保證；正負號代表方向，|IC| 越大關聯越強。"
                              "每日橫斷面 ICIR 見 daily 區塊。"}
    except Exception as e:
        logger.warning(f"factor_ic_report: {e}")
        return {"ok": False, "error": str(e)}


def _rules(rej_closed, cand_closed):
    """每條規則擋下的假想單，後來的表現如何。
    ★ 2026-11-11 SH06：依 (reject 原因, 方向) 分組——同一原因（如量能不足）可能含多空，
    不可取第一筆方向代表整組。結論措辭改為描述性（此組假想單均值與 0 比較），不逕稱「規則在
    保護你」（那是對 0 比較、非與放行對照；是否真有增益須看 vs_baseline_z 並經前向 Gate ablation）。"""
    by = {}
    for r in rej_closed:
        by.setdefault((_f(r, "reject"), r.get("direction")), []).append(r)
    out = []
    for (code, direction), xs in by.items():
        st = _stats(xs)
        base = [c for c in cand_closed if c["direction"] == direction]
        item = dict(st, code=code, label=_readable_reason(code), direction=direction, baseline_n=len(base))
        if st["n"] < MIN_N:
            item["verdict"] = f"樣本不足（{st['n']}/{MIN_N}）"
            item["tone"] = "wait"
        else:
            t = st.get("t_vs0") or 0
            if t >= Z_TENT and (st["avg_r"] or 0) > 0:
                item["verdict"] = f"此組被擋下的假想單歷史模擬平均 R {st['avg_r']:+.2f}（與 0 比較為正）——需與同向放行候選對照（見 vs_baseline_z）才能判定規則是否過嚴"
                item["tone"] = "loose"
            elif t <= -Z_TENT:
                item["verdict"] = f"此組被擋下的假想單歷史模擬平均 R {st['avg_r']:+.2f}（與 0 比較為負）——僅代表此組均值為負，非與放行對照的增益證明"
                item["tone"] = "good"
            else:
                item["verdict"] = "與 0 沒有顯著差異，暫無結論"
                item["tone"] = "wait"
            if len(base) >= MIN_N:
                item["vs_baseline_z"] = round(_zr(_rl(xs), _rl(base)), 2)
        out.append(item)
    out.sort(key=lambda z: -z["n"])
    return out


def _max_dd(path):
    """累積 R 路徑的最大回撤（≤0，以 R 為單位）。"""
    peak = cum = dd = 0.0
    for r in path:
        cum += r; peak = max(peak, cum); dd = min(dd, cum - peak)
    return dd


def _mc_equity(rows, n_sims=2000, seed=BOOT_SEED):
    """蒙地卡羅（本期經驗分布 IID 重抽樣）：把已結算的每筆淨 R 當母體，重抽 N 次組成同筆數路徑，
    得到『總 R、本次模擬正值比例、最大回撤、最長連敗』的分布——看策略在本期樣本下的運氣區間。
    ★ 誠實限制（SH07）：IID 逐筆重抽、未含交易日區塊/行情分層/重疊持倉/資金約束；為敏感度參考、
    非未來真實機率；R 累積非可成交帳戶權益。"""
    rs = _rl([r for r in rows if r.get("result")])
    n = len(rs)
    if n < MIN_N:
        return {"ready": False, "n": n, "need": MIN_N}
    rng = random.Random(seed)
    finals, dds, streaks = [], [], []
    for _ in range(n_sims):
        path = [rs[rng.randrange(n)] for _ in range(n)]
        finals.append(sum(path)); dds.append(_max_dd(path))
        s = c = 0
        for r in path:
            c = c + 1 if r < 0 else 0
            s = max(s, c)
        streaks.append(s)
    finals.sort(); dds.sort(); streaks.sort()
    q = lambda a, p: a[min(len(a) - 1, int(p * len(a)))]
    pos = sum(1 for x in finals if x > 0)
    return {"ready": True, "n": n, "n_sims": n_sims,
            "per_trade_r": round(sum(rs) / n, 3),
            "total_r_med": round(q(finals, .5), 1), "total_r_lo": round(q(finals, .05), 1),
            "total_r_hi": round(q(finals, .95), 1),
            # ★ 2026-11-11 SH07：本次模擬正值比例（非未來真實機率）。pos/n_sims 的實際計數也回傳，
            # 讓 0% 能誠實顯示為「本次 N 條模擬無正值」，不被解讀成未來獲利機率＝0。
            "prob_profit": round(pos / len(finals) * 100, 1),
            "sims_positive": pos,
            "maxdd_med": round(q(dds, .5), 1), "maxdd_worst5": round(q(dds, .05), 1),
            "streak_med": q(streaks, .5), "streak_worst5": q(streaks, .95),
            # 方法論誠實揭露：IID 逐筆重抽本期經驗分布，未含交易日區塊/行情分層/重疊持倉/資金約束；
            # R 累積非可成交帳戶權益；僅作運氣區間敏感度，非未來真實機率。
            "method": "iid_resample_current",
            "note": "本期經驗分布 IID 重抽樣（未含交易日區塊/資金約束）；為敏感度參考、非未來真實機率；R 累積非帳戶權益"}


def _equity_curve(rows, cap=300):
    """已結算候選依訊號日累積的淨 R 曲線（供前端畫圖）。
    ★ SH08：這是「按決策日(訊號日)歸因」的累積淨 R，不是實際可見/可成交的帳戶資金曲線——
    未成熟批次的 R 會回填到過去日期。真實帳戶資金曲線見績效頁。"""
    cl = [r for r in rows if r.get("status") == "closed" and r.get("r_multiple") is not None]
    cl.sort(key=lambda r: (r["bar_date"], r.get("id", 0)))
    cum, pts = 0.0, []
    for r in cl:
        cum += r["r_multiple"]; pts.append({"date": r["bar_date"], "cum_r": round(cum, 2)})
    if len(pts) > cap:
        step = len(pts) // cap + 1
        pts = [p for i, p in enumerate(pts) if i % step == 0 or i == len(pts) - 1]
    return pts


def _monthly(rows):
    """逐月績效（已結算）：筆數、平均 R、勝率、累積 R。"""
    by = {}
    for r in rows:
        if r.get("r_multiple") is None:
            continue
        by.setdefault(r["bar_date"][:7], []).append(r)
    out = []
    for m in sorted(by):
        st = _stats(by[m])
        out.append({"month": m, "n": st["n"], "avg_r": st["avg_r"],
                    "win_rate": st["win_rate"], "total_r": st["total_r"]})
    return out


def _unresolved_breakdown(rows):
    """★ 2026-11-11 SH04：把 unresolved 依 result 原因拆分（nodata＝逾期無 K 棒可結算、
    bad_risk＝進場≈停損風險退化）。離線無法區分退市/停牌/資料源中斷，故不逕稱退市。"""
    ub = {}
    for r in rows:
        if r.get("status") == "unresolved":
            key = r.get("result") or "unknown"
            ub[key] = ub.get(key, 0) + 1
    return ub


def build_report():
    rows = _rows()
    cand = [r for r in rows if r["kind"] == "candidate"]
    ctrl = [r for r in rows if r["kind"] == "control"]
    rej = [r for r in rows if r["kind"] == "rejected"]
    closed = lambda xs: [r for r in xs if r["status"] == "closed"]
    cc, kc, rc = closed(cand), closed(ctrl), closed(rej)
    sent = [r for r in cc if r.get("sent")]
    dates = sorted({r["bar_date"] for r in rows})
    closed_days = len({r["bar_date"] for r in cc})
    out = {
        "generated_at": (datetime.now(timezone(timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M:%S"),
        "progress": {"days": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
                     "candidates": len(cand), "controls": len(ctrl), "rejected": len(rej),
                     "closed_candidates": len(cc), "closed_controls": len(kc), "closed_rejected": len(rc),
                     "closed_days": closed_days,
                     "pending": sum(1 for r in rows if r["status"] == "pending"),
                     "unresolved": sum(1 for r in rows if r["status"] == "unresolved"),   # 無法結算，已排除在勝率與平均 R 之外
                     "unresolved_breakdown": _unresolved_breakdown(rows),                 # ★ SH04：原因拆分（nodata/bad_risk）
                     "min_n": MIN_N, "min_days": MIN_DAYS},
        "groups": {"候選（全部，含沒發出的）": _stats(cc), "實際發出": _stats(sent), "對照組（隨機做多）": _stats(kc),
                   "被規則擋下（假想）": _stats(rc)},
        "dimensions": [], "findings": [], "rules": _rules(rc, cc),
        "montecarlo": _mc_equity(cc), "equity_curve": _equity_curve(cc), "monthly": _monthly(cc),
        # ★ 2026-11-11 SH08：此曲線是「已成熟訊號累積淨 R，按決策日(訊號日)歸因」，不是實際可見/
        # 可成交的帳戶資金曲線（未成熟批次的 R 會回填到過去日期）。帳戶資金曲線見績效頁。
        "equity_curve_note": "累積淨 R 按決策日(訊號日)歸因；非帳戶資金曲線，未成熟批次的 R 會回填到過去日期。",
    }
    reliable = closed_days >= MIN_DAYS
    out["reliable"] = reliable
    # 主要指標固定為「平均 R」，以交易日為叢集做兩樣本拔靴（修正同日訊號偽獨立使 z 值灌水），
    # 全部分組的 p 值再用 Benjamini-Hochberg 控制偽發現率（修正多重比較）。勝率只作輔助顯示。
    cds = sorted({r["bar_date"] for r in cc})
    mid = cds[len(cds) // 2] if len(cds) >= 4 else None
    # 各主要群組的平均 R 以拔靴附上 95% 信賴區間
    for gname, grows in (("候選（全部，含沒發出的）", cc), ("實際發出", sent), ("對照組（隨機做多）", kc), ("被規則擋下（假想）", rc)):
        m, lo, hi = _boot_ci(grows)
        out["groups"][gname].update(avg_r_lo=lo, avg_r_hi=hi)
    pending_tests = []   # (dim_name, item_dict, xs, rest) 蒐集所有『樣本足夠』的分組，之後統一做 FDR
    for name, fn in _dims():
        buckets = {}
        for r in cc:
            k = fn(r)
            if k is not None:
                buckets.setdefault(k, []).append(r)
        if len(buckets) < 2:
            continue
        items = []
        for k, xs in buckets.items():
            st = _stats(xs)
            ids = {id(x) for x in xs}
            rest = [r for r in cc if id(r) not in ids]
            enough = st["n"] >= MIN_N and len(rest) >= MIN_N
            diff, p, z = (_boot_diff(xs, rest) if enough else (None, 1.0, 0.0))
            m_lo = _boot_ci(xs)
            st.update(label=k, z=z, diff_r=diff, p=p, avg_r_lo=m_lo[1], avg_r_hi=m_lo[2],
                      enough=st["n"] >= MIN_N, level="", halves=None, consistent=None)
            items.append(st)
            if enough and reliable and p is not None:
                pending_tests.append((name, st, xs, rest))
        items.sort(key=lambda z: z["label"])
        out["dimensions"].append({"name": name, "items": items})
    # Benjamini-Hochberg FDR 跨所有分組
    if pending_tests:
        flags = _bh_reject([it["p"] for (_, it, _, _) in pending_tests], FDR_Q)
        for (name, st, xs, rest), sig in zip(pending_tests, flags):
            z = st["z"]
            # 時間穩定性：已結算樣本依日期對半切，前後兩段方向一致才升級為「較可靠」
            halves, consistent = None, None
            if mid:
                hs = []
                for part in (lambda r: r["bar_date"] < mid, lambda r: r["bar_date"] >= mid):
                    a_ = [r for r in xs if part(r)]; b_ = [r for r in rest if part(r)]
                    d_, p_, _z_ = _boot_diff(a_, b_) if (len(a_) >= 10 and len(b_) >= 10) else (None, 1.0, 0.0)
                    hs.append(d_)
                halves = hs
                consistent = all(v is not None and v * z > 0 for v in hs) if z else False
            level = "strong" if (sig and consistent) else ("tent" if st["p"] < 0.05 else "")
            st.update(level=level, halves=halves, consistent=consistent)
            if level:
                out["findings"].append({
                    "dim": name, "label": st["label"], "n": st["n"], "win_rate": st["win_rate"], "avg_r": st["avg_r"],
                    "avg_r_lo": st["avg_r_lo"], "avg_r_hi": st["avg_r_hi"], "p": st["p"], "z": z,
                    "level": level, "halves": halves, "consistent": consistent,
                    "text": f"【{name}】{st['label']}：平均 R {st['avg_r']}（95% CI {st['avg_r_lo']}~{st['avg_r_hi']}）、"
                            f"勝率 {st['win_rate']}%（n={st['n']}，p={st['p']}），{'明顯優於' if z > 0 else '明顯劣於'}其餘樣本"})
    # ★ 2026-11-08（TS-SHORT-001 修正）：對照組是「隨機做多」(long-only)，故與它比較的候選也必須
    # 只取「做多」，否則候選含放空樣本時，這條「訊號是否優於隨機」的結論會是方向組成混淆
    # （mixed-direction 候選 對 long-only 對照）。放空沒有對應的隨機空單對照組，故放空不納入這條
    # 比較；放空的獨立表現見「方向」維度與各別統計。見 docs/ai-governance/LONGSHORT_SEPARATION_AUDIT.md。
    cc_buy = [r for r in cc if r.get("direction") == "buy"]
    a, b = _stats(cc_buy), out["groups"]["對照組（隨機做多）"]
    if a.get("n", 0) >= MIN_N and b.get("n", 0) >= MIN_N:
        diff, p, z = _boot_diff(cc_buy, kc)
        sig = p is not None and p < 0.05
        out["edge"] = {"z": z, "p": p, "diff_r": diff, "significant": sig, "direction": "buy",
                       "text": ("做多訊號平均 R " + ("顯著優於" if (sig and diff > 0) else "顯著劣於" if (sig and diff < 0) else "與")
                                + "隨機做多對照組" + ("" if sig else "沒有顯著差異")
                                + f"（{a['avg_r']} vs {b['avg_r']}；差 {diff}，p={p}；勝率 {a['win_rate']}% vs {b['win_rate']}%）"
                                + "（放空無隨機對照、不納入本比較，見「方向」維度）")}
    else:
        out["edge"] = {"text": f"樣本不足（做多候選 {a.get('n', 0)}、隨機做多對照 {b.get('n', 0)}，各需 ≥ {MIN_N} 筆已結算）才能判斷做多訊號有沒有比隨機好。"}
    out["findings"].sort(key=lambda f: (f["level"] != "strong", -abs(f["z"] or 0)))
    out["note"] = (f"目前已結算的資料涵蓋 {closed_days} 個交易日；需至少 {MIN_DAYS} 日才列出結論。"
                   if not reliable else "")
    return out
