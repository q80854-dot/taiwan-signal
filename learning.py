"""
策略學習報告：從 shadow_signals 的已結算資料，用誠實的統計方式回答「哪些條件有效」。

規則：
- 勝＝觸及任一停利（tp1/tp2/tp3）；敗＝停損；expired（逾期未到價）另計，但仍計入平均 R。
- 樣本 < 30 的分組只顯示、不下結論。
- 結論只在「該分組樣本 ≥ 30、且勝率與其餘樣本差異達 95% 信賴（雙比例 z 檢定）」時才列出。
- 同時列出對照組（沒有訊號、隨機抽樣、同一套停損停利機制），回答「訊號有沒有比隨便買好」。
"""
import json, math
from datetime import datetime, timezone, timedelta

MIN_N = 30
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


def _stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0}
    w = sum(1 for r in rows if r["result"] in WIN)
    l = sum(1 for r in rows if r["result"] == "sl")
    e = n - w - l
    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    r10 = [r["r10"] for r in rows if r.get("r10") is not None]
    return {"n": n, "win": w, "loss": l, "expired": e,
            "win_rate": round(w / n * 100, 1), "stop_rate": round(l / n * 100, 1),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "avg_r10": round(sum(r10) / len(r10), 3) if r10 else None, "n_r10": len(r10),
            "mfe": round(sum(r["mfe_r"] for r in rows if r.get("mfe_r") is not None) / max(1, sum(1 for r in rows if r.get("mfe_r") is not None)), 2)}


def _z(w1, n1, w2, n2):
    if n1 < 1 or n2 < 1:
        return 0.0
    p = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) or 1e-9
    return (w1 / n1 - w2 / n2) / se


def _band(v, edges, labels):
    if v is None:
        return None
    for e, lb in zip(edges, labels):
        if v < e:
            return lb
    return labels[-1]


def _dims():
    return [
        ("分數", lambda r: _band(r.get("score"), [70, 75, 80, 85], ["65–69", "70–74", "75–79", "80–84", "85+"])),
        ("追高等級", lambda r: {"low": "低", "mid": "中", "high": "高"}.get(r["f"].get("chase_level"))),
        ("基本面等級", lambda r: r["f"].get("f_grade") or "未評分"),
        ("基本面分數", lambda r: _band(r["f"].get("f_score"), [35, 50, 65, 80], ["<35", "35–49", "50–64", "65–79", "80+"])),
        ("營收年增", lambda r: _band(r["f"].get("rev_yoy"), [0, 10, 25, 50], ["衰退", "0–10%", "10–25%", "25–50%", "50%+"])),
        ("ADX", lambda r: _band(r["f"].get("adx_value"), [20, 25, 30, 40], ["<20", "20–25", "25–30", "30–40", "40+"])),
        ("RSI", lambda r: _band(r["f"].get("rsi_value"), [40, 50, 60, 70], ["<40", "40–50", "50–60", "60–70", "70+"])),
        ("量比", lambda r: _band(r["f"].get("vol_ratio"), [1.2, 1.5, 2, 3], ["<1.2", "1.2–1.5", "1.5–2", "2–3", "3+"])),
        ("停損距離(ATR)", lambda r: _band(r["f"].get("sl_atr"), [1.5, 2, 2.5, 3], ["<1.5", "1.5–2", "2–2.5", "2.5–3", "3+"])),
        ("離均線延伸(ATR)", lambda r: _band(r["f"].get("ext_atr"), [1, 2, 3], ["<1", "1–2", "2–3", "3+"])),
        ("週線", lambda r: r["f"].get("weekly_bias")),
        ("規模", lambda r: r.get("size_cat")),
        ("大盤情緒", lambda r: _band(r["f"].get("sentiment"), [40, 60], ["偏弱<40", "中性", "偏強≥60"])),
        ("星期", lambda r: "一二三四五六日"[int(r["f"]["dow"])] if r["f"].get("dow") is not None else None),
        ("市場", lambda r: "上櫃" if (r.get("ticker") or "").endswith(".TWO") else "上市"),
    ]


def build_report():
    rows = _rows()
    cand = [r for r in rows if r["kind"] == "candidate"]
    ctrl = [r for r in rows if r["kind"] == "control"]
    closed = lambda xs: [r for r in xs if r["status"] == "closed"]
    cc, kc = closed(cand), closed(ctrl)
    sent = [r for r in cc if r.get("sent")]
    dates = sorted({r["bar_date"] for r in rows})
    out = {
        "generated_at": (datetime.now(timezone(timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M:%S"),
        "progress": {"days": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
                     "candidates": len(cand), "controls": len(ctrl),
                     "closed_candidates": len(cc), "closed_controls": len(kc),
                     "pending": sum(1 for r in rows if r["status"] == "pending"), "min_n": MIN_N},
        "groups": {"候選（全部，含沒發出的）": _stats(cc), "實際發出": _stats(sent), "對照組（隨機做多）": _stats(kc)},
        "dimensions": [], "findings": [],
    }
    base_w, base_n = sum(1 for r in cc if r["result"] in WIN), len(cc)
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
            w = st["win"]
            rest_w, rest_n = base_w - w, base_n - st["n"]
            z = _z(w, st["n"], rest_w, rest_n) if st["n"] >= MIN_N and rest_n >= MIN_N else 0.0
            st.update(label=k, z=round(z, 2), enough=st["n"] >= MIN_N)
            items.append(st)
            if st["enough"] and abs(z) >= 1.96:
                out["findings"].append({
                    "dim": name, "label": k, "n": st["n"], "win_rate": st["win_rate"], "avg_r": st["avg_r"], "z": round(z, 2),
                    "text": f"【{name}】{k}：勝率 {st['win_rate']}%（n={st['n']}），{'明顯高於' if z > 0 else '明顯低於'}其餘樣本（平均 R {st['avg_r']}）"})
        out["dimensions"].append({"name": name, "items": items})
    # 候選 vs 對照
    a, b = out["groups"]["候選（全部，含沒發出的）"], out["groups"]["對照組（隨機做多）"]
    if a.get("n", 0) >= MIN_N and b.get("n", 0) >= MIN_N:
        z = _z(a["win"], a["n"], b["win"], b["n"])
        out["edge"] = {"z": round(z, 2), "significant": abs(z) >= 1.96,
                       "text": ("訊號勝率" + ("顯著高於" if z >= 1.96 else "顯著低於" if z <= -1.96 else "與") + "隨機對照組"
                                + ("" if abs(z) >= 1.96 else "沒有顯著差異") + f"（候選 {a['win_rate']}% vs 對照 {b['win_rate']}%）")}
    else:
        out["edge"] = {"text": f"樣本不足（候選 {a.get('n', 0)}、對照 {b.get('n', 0)}，各需 ≥ {MIN_N} 筆已結算）才能判斷訊號有沒有比隨機好。"}
    out["findings"].sort(key=lambda f: -abs(f["z"]))
    return out
