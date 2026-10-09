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
import json, math, random
from datetime import datetime, timezone, timedelta

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
    t = round(m / (sd / math.sqrt(len(rs))), 2) if len(rs) > 1 and sd > 0 else None
    return {"n": n, "win": w, "loss": l, "expired": e,
            "win_rate": round(w / n * 100, 1), "stop_rate": round(l / n * 100, 1),
            "avg_r": round(m, 3) if rs else None, "t_vs0": t,
            "avg_r10": round(sum(r10) / len(r10), 3) if r10 else None, "n_r10": len(r10),
            "mfe": round(sum(mf) / len(mf), 2) if mf else None}


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
    frac_le = sum(1 for d in diffs if d <= 0) / len(diffs)
    frac_ge = sum(1 for d in diffs if d >= 0) / len(diffs)
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
    ]


def _readable_reason(code):
    try:
        import shadow
        return shadow.REASONS.get(code, code)
    except Exception:
        return code


def _rules(rej_closed, cand_closed):
    """每條規則擋下的假想單，後來的表現如何。"""
    by = {}
    for r in rej_closed:
        by.setdefault(_f(r, "reject"), []).append(r)
    out = []
    for code, xs in by.items():
        st = _stats(xs)
        base = [c for c in cand_closed if c["direction"] == xs[0]["direction"]]
        item = dict(st, code=code, label=_readable_reason(code), direction=xs[0]["direction"], baseline_n=len(base))
        if st["n"] < MIN_N:
            item["verdict"] = f"樣本不足（{st['n']}/{MIN_N}）"
            item["tone"] = "wait"
        else:
            t = st.get("t_vs0") or 0
            if t >= Z_TENT and (st["avg_r"] or 0) > 0:
                item["verdict"] = f"被擋下的單其實有正期望值（平均 R {st['avg_r']:+.2f}）——這條規則可能過嚴，建議觀察更多樣本"
                item["tone"] = "loose"
            elif t <= -Z_TENT:
                item["verdict"] = f"被擋下的單期望值為負（平均 R {st['avg_r']:+.2f}）——這條規則在保護你"
                item["tone"] = "good"
            else:
                item["verdict"] = "與 0 沒有顯著差異，暫無結論"
                item["tone"] = "wait"
            if len(base) >= MIN_N:
                item["vs_baseline_z"] = round(_zr(_rl(xs), _rl(base)), 2)
        out.append(item)
    out.sort(key=lambda z: -z["n"])
    return out


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
                     "unresolved": sum(1 for r in rows if r["status"] == "unresolved"),   # 退市／停牌無法結算，已排除在勝率與平均 R 之外
                     "min_n": MIN_N, "min_days": MIN_DAYS},
        "groups": {"候選（全部，含沒發出的）": _stats(cc), "實際發出": _stats(sent), "對照組（隨機做多）": _stats(kc),
                   "被規則擋下（假想）": _stats(rc)},
        "dimensions": [], "findings": [], "rules": _rules(rc, cc),
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
    a, b = out["groups"]["候選（全部，含沒發出的）"], out["groups"]["對照組（隨機做多）"]
    if a.get("n", 0) >= MIN_N and b.get("n", 0) >= MIN_N:
        diff, p, z = _boot_diff(cc, kc)
        sig = p is not None and p < 0.05
        out["edge"] = {"z": z, "p": p, "diff_r": diff, "significant": sig,
                       "text": ("訊號平均 R " + ("顯著優於" if (sig and diff > 0) else "顯著劣於" if (sig and diff < 0) else "與")
                                + "隨機對照組" + ("" if sig else "沒有顯著差異")
                                + f"（{a['avg_r']} vs {b['avg_r']}；差 {diff}，p={p}；勝率 {a['win_rate']}% vs {b['win_rate']}%）")}
    else:
        out["edge"] = {"text": f"樣本不足（候選 {a.get('n', 0)}、對照 {b.get('n', 0)}，各需 ≥ {MIN_N} 筆已結算）才能判斷訊號有沒有比隨機好。"}
    out["findings"].sort(key=lambda f: (f["level"] != "strong", -abs(f["z"] or 0)))
    out["note"] = (f"目前已結算的資料涵蓋 {closed_days} 個交易日；需至少 {MIN_DAYS} 日才列出結論。"
                   if not reliable else "")
    return out
