"""
策略學習報告：從 shadow_signals 的已結算資料，用誠實的統計方式回答「哪些條件有效」。

規則：
- 勝＝觸及任一停利（tp1/tp2/tp3）；敗＝停損；expired（逾期未到價）另計，但仍計入平均 R。
- 主要看「平均 R（期望值）」，勝率只是輔助——因為停損與停利的距離不同，勝率高不代表賺錢。
- 樣本 < 30 的分組只顯示、不下結論。
- 多重比較防護：同時檢驗幾十個分組，純靠運氣也會有幾個「達標」。所以
    * 累積不到 10 個交易日，一律不列結論；
    * z ≥ 1.96 只標示「初步（需新樣本再驗證）」；z ≥ 3.2（約等於對 70 個比較做 Bonferroni 校正）且前後兩段時間方向一致，才標示「較可靠」。
- 對照組（沒有訊號、隨機抽樣、同一套停損停利機制）回答「訊號有沒有比隨便買好」。
- 被規則擋下的假想單（kind=rejected）回答「這條規則是幫了我、還是擋掉了賺錢的單」。
"""
import json, math
from datetime import datetime, timezone, timedelta

MIN_N = 30
MIN_DAYS = 10
Z_TENT, Z_STRONG = 1.96, 3.2
WIN = ("tp1", "tp2", "tp3")


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
    base_w, base_n = sum(1 for r in cc if r["result"] in WIN), len(cc)
    base_r = _rl(cc)
    # 時間穩定性：把已結算樣本依日期對半切，一個真的有效的條件在前後兩段都應該同方向
    cds = sorted({r["bar_date"] for r in cc})
    mid = cds[len(cds) // 2] if len(cds) >= 4 else None
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
            rest_w, rest_n = base_w - st["win"], base_n - st["n"]
            enough = st["n"] >= MIN_N and rest_n >= MIN_N
            zw = _z(st["win"], st["n"], rest_w, rest_n) if enough else 0.0
            zrr = _zr(_rl(xs), _rl(rest)) if enough else 0.0
            z = zw if abs(zw) >= abs(zrr) else zrr
            level = "strong" if abs(z) >= Z_STRONG else "tent" if abs(z) >= Z_TENT else ""
            halves, consistent = None, None
            if enough and mid and level:
                zs = []
                for part in (lambda r: r["bar_date"] < mid, lambda r: r["bar_date"] >= mid):
                    a_ = [r for r in xs if part(r)]
                    b_ = [r for r in rest if part(r)]
                    zs.append(round(_zr(_rl(a_), _rl(b_)), 2) if len(a_) >= 10 and len(b_) >= 10 else None)
                halves = zs
                consistent = all(v is not None and v * z > 0 and abs(v) >= 1.0 for v in zs)
                if level == "strong" and not consistent:
                    level = "tent"          # 前後兩段不一致，不給「較可靠」
            st.update(halves=halves, consistent=consistent, label=k, z=round(z, 2), z_win=round(zw, 2), z_r=round(zrr, 2), enough=st["n"] >= MIN_N,
                      level=level if (enough and reliable) else "")
            items.append(st)
            if enough and reliable and level:
                out["findings"].append({
                    "dim": name, "label": k, "n": st["n"], "win_rate": st["win_rate"], "avg_r": st["avg_r"],
                    "z": round(z, 2), "level": level, "halves": halves, "consistent": consistent,
                    "text": f"【{name}】{k}：勝率 {st['win_rate']}%、平均 R {st['avg_r']}（n={st['n']}），"
                            f"{'明顯優於' if z > 0 else '明顯劣於'}其餘樣本"})
        items.sort(key=lambda z: z["label"])
        out["dimensions"].append({"name": name, "items": items})
    a, b = out["groups"]["候選（全部，含沒發出的）"], out["groups"]["對照組（隨機做多）"]
    if a.get("n", 0) >= MIN_N and b.get("n", 0) >= MIN_N:
        z = _z(a["win"], a["n"], b["win"], b["n"])
        zr_ = _zr(_rl(cc), _rl(kc))
        sig = abs(z) >= Z_TENT or abs(zr_) >= Z_TENT
        out["edge"] = {"z": round(z, 2), "z_r": round(zr_, 2), "significant": sig,
                       "text": ("訊號" + ("顯著優於" if (z >= Z_TENT or zr_ >= Z_TENT) else "顯著劣於" if (z <= -Z_TENT or zr_ <= -Z_TENT) else "與")
                                + "隨機對照組" + ("" if sig else "沒有顯著差異")
                                + f"（勝率 {a['win_rate']}% vs {b['win_rate']}%；平均 R {a['avg_r']} vs {b['avg_r']}）")}
    else:
        out["edge"] = {"text": f"樣本不足（候選 {a.get('n', 0)}、對照 {b.get('n', 0)}，各需 ≥ {MIN_N} 筆已結算）才能判斷訊號有沒有比隨機好。"}
    out["findings"].sort(key=lambda f: (f["level"] != "strong", -abs(f["z"])))
    out["note"] = (f"目前已結算的資料涵蓋 {closed_days} 個交易日；需至少 {MIN_DAYS} 日才列出結論。"
                   if not reliable else "")
    return out
