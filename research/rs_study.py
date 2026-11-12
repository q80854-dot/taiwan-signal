"""相對強弱研究（研究工具；不被 app 匯入、不影響任何線上行為）。

問題：重播顯示 2024 年後做多訊號的股票在進場後落後大盤——現有的技術條件選不到領漲股。
若在現有做多訊號上再加一條「相對強弱」條件（只留比大盤或比其他股票強的），期望值會不會改善？

做法：取 champion_replay.py 產出的做多訊號（已定案者），用訊號日（含）以前的還原收盤價計算
相對強弱特徵，比較「留下組」與「全部」的 Champion 淨 R。特徵只用訊號日當下已知的資料。

濾網在看結果之前就寫死在 FILTERS（6 個），門檻取慣用的整數（0、70 百分位、九成），不依結果調整。
判定方式與出場研究相同、事前寫死：
  1. 只在 2020–2023 挑：『留下組平均 − 全部平均』最高、且其 Bonferroni 區間下界 > 0 者。
  2. 到 2024–2026 確認：同一濾網的差異 95% 區間下界仍 > 0 才算成立。
  3. 區間以「訊號日」為叢集做成對拔靴（同一天的訊號高度相關）。
另外一併列出留下組本身的平均淨 R 與區間——濾網「比較好」不等於「會賺」，兩個都要看。

橫斷面排名的母體＝抽樣清單中當天有足夠歷史的股票；清單含已下市代號但抓不到其股價，
排名因此略偏向存活者，解讀時須一併考慮。

用法：python research/rs_study.py --signals replay.csv --cache-dir hist/ [--out rs_study.json]
"""
import argparse
import bisect
import csv
import json
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPLIT = "2024-01-01"
N_BOOT = 2000
SEED = 20261112

FILTERS = [
    {"id": "R1", "name": "近 20 日漲幅勝過加權指數", "keep": lambda f: _gt(f.get("rs20"), 0)},
    {"id": "R2", "name": "近 60 日漲幅勝過加權指數", "keep": lambda f: _gt(f.get("rs60"), 0)},
    {"id": "R3", "name": "近 120 日漲幅勝過加權指數", "keep": lambda f: _gt(f.get("rs120"), 0)},
    {"id": "R4", "name": "近 60 日漲幅排在全體前三成", "keep": lambda f: _ge(f.get("rank60"), 70)},
    {"id": "R5", "name": "近 120 日漲幅排在全體前三成", "keep": lambda f: _ge(f.get("rank120"), 70)},
    {"id": "R6", "name": "收盤在近 250 日最高價的九成以上", "keep": lambda f: _ge(f.get("near_high"), 0.9)},
]


def _gt(x, t):
    return None if x is None else x > t


def _ge(x, t):
    return None if x is None else x >= t


def _ret(closes, i, n):
    if i - n < 0 or not closes[i - n] or not closes[i]:
        return None
    return closes[i] / closes[i - n] - 1


def build_features(sigs, hist, index):
    """sigs: [{ticker, signal_date, r}]；hist: {ticker: {dates, closes, highs}}；index: 加權指數同形狀。
    回傳與 sigs 等長的特徵清單。缺資料的特徵為 None（不猜）。"""
    pos = {tk: {d: i for i, d in enumerate(h["dates"])} for tk, h in hist.items()}
    ipos = {d: i for i, d in enumerate(index["dates"])}
    need = sorted({s["signal_date"] for s in sigs})
    xs = {}                                              # 橫斷面：{日期: {n: 已排序的全體 n 日報酬}}
    for day in need:
        row = {}
        for n in (60, 120):
            vals = []
            for tk, h in hist.items():
                i = pos[tk].get(day)
                if i is not None:
                    r = _ret(h["closes"], i, n)
                    if r is not None:
                        vals.append(r)
            row[n] = sorted(vals)
        xs[day] = row
    out = []
    for s in sigs:
        f = {}
        h, day = hist.get(s["ticker"]), s["signal_date"]
        i = pos.get(s["ticker"], {}).get(day) if h else None
        j = ipos.get(day)
        if i is not None:
            for n in (20, 60, 120):
                a = _ret(h["closes"], i, n)
                b = _ret(index["closes"], j, n) if j is not None else None
                f[f"rs{n}"] = None if a is None or b is None else a - b
                if n in (60, 120):
                    pool = xs[day][n]
                    f[f"rank{n}"] = (None if a is None or len(pool) < 30
                                     else bisect.bisect_left(pool, a) / len(pool) * 100)
            if i >= 249:
                hh = max(x for x in h["highs"][i - 249:i + 1] if x)
                f["near_high"] = h["closes"][i] / hh if hh else None
        out.append(f)
    return out


def _boot_diff(kept, base, n_boot=N_BOOT, seed=SEED, alpha=0.05):
    """成對（同一批訊號日）叢集拔靴：留下組平均 − 全部平均。回傳 (差, 下界, 上界)。"""
    days = sorted(base)
    kv = [x for d in days for x in kept.get(d, [])]
    bv = [x for d in days for x in base[d]]
    if not kv or not bv:
        return None, None, None
    diff = sum(kv) / len(kv) - sum(bv) / len(bv)
    if len(days) < 20:
        return diff, None, None
    rng = random.Random(seed)
    ds = []
    for _ in range(n_boot):
        ks = kn = bs = bn = 0
        for _ in range(len(days)):
            d = days[rng.randrange(len(days))]
            k, b = kept.get(d, ()), base[d]
            ks += sum(k); kn += len(k); bs += sum(b); bn += len(b)
        if kn and bn:
            ds.append(ks / kn - bs / bn)
    if len(ds) < 100:
        return diff, None, None
    ds.sort()
    return diff, ds[int(alpha / 2 * len(ds))], ds[min(len(ds) - 1, int((1 - alpha / 2) * len(ds)))]


def _boot_mean(by_day, n_boot=N_BOOT, seed=SEED):
    d, lo, hi = _boot_diff(by_day, {k: [0.0] for k in by_day}, n_boot, seed)
    return d, lo, hi


def summarize(sigs, feats):
    out = {}
    for flt in FILTERS:
        o = {"name": flt["name"]}
        for label, keep in (("train", lambda sd: sd < SPLIT), ("test", lambda sd: sd >= SPLIT)):
            base, kept, n_missing = defaultdict(list), defaultdict(list), 0
            for s, f in zip(sigs, feats):
                if not keep(s["signal_date"]):
                    continue
                k = flt["keep"](f)
                if k is None:
                    n_missing += 1                       # 特徵缺漏：兩組皆不計
                    continue
                base[s["signal_date"]].append(s["r"])
                if k:
                    kept[s["signal_date"]].append(s["r"])
            bv = [x for v in base.values() for x in v]
            kv = [x for v in kept.values() for x in v]
            d, lo, hi = _boot_diff(kept, base)
            _, blo, bhi = _boot_diff(kept, base, alpha=0.05 / len(FILTERS))
            km, klo, khi = _boot_mean(kept) if kv else (None, None, None)
            o[label] = {
                "n_base": len(bv), "n_kept": len(kv), "n_missing": n_missing,
                "kept_share": round(len(kv) / len(bv) * 100, 1) if bv else None,
                "base_avg_r": round(sum(bv) / len(bv), 4) if bv else None,
                "kept_avg_r": round(km, 4) if km is not None else None,
                "kept_avg_r_ci95": [round(klo, 4), round(khi, 4)] if klo is not None else None,
                "dropped_avg_r": (round((sum(bv) - sum(kv)) / (len(bv) - len(kv)), 4) if len(bv) > len(kv) else None),
                "diff_vs_base": round(d, 4) if d is not None else None,
                "diff_ci95": [round(lo, 4), round(hi, 4)] if lo is not None else None,
                "diff_ci_bonferroni": [round(blo, 4), round(bhi, 4)] if blo is not None else None,
            }
        out[flt["id"]] = o
    cand = [(out[f["id"]]["train"]["diff_vs_base"], f["id"]) for f in FILTERS
            if out[f["id"]]["train"]["diff_ci_bonferroni"] and out[f["id"]]["train"]["diff_ci_bonferroni"][0] > 0]
    verdict = {"rule": "訓練期差異的 Bonferroni 區間下界 > 0 者中取差異最大；驗證期差異 95% 區間下界 > 0 才成立"}
    if not cand:
        verdict.update(selected=None, result="none_selected",
                       note="訓練期沒有任何相對強弱濾網顯著優於不設濾網；不進入驗證。")
    else:
        best = max(cand)[1]
        t = out[best]["test"]
        ok = bool(t["diff_ci95"] and t["diff_ci95"][0] > 0)
        pos = bool(t["kept_avg_r_ci95"] and t["kept_avg_r_ci95"][0] > 0)
        verdict.update(selected=best, result="confirmed" if ok else "not_confirmed", kept_group_positive_in_test=pos,
                       note=(f"{best} 在驗證期：留下組 {t['kept_avg_r']}R（95% 區間 {t['kept_avg_r_ci95']}），"
                             f"比全部高 {t['diff_vs_base']}R（95% 區間 {t['diff_ci95']}）。"
                             + ("樣本外仍較好" if ok else "樣本外未能確認")
                             + ("，且留下組本身為正。" if pos else "；但留下組本身的期望值仍未顯著為正。")))
    return {"filters": out, "verdict": verdict}


def _load(signals_path, cache_dir):
    from research.champion_replay import load_history
    sigs = []
    with open(signals_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("direction") == "buy" and r.get("status") == "final" and r.get("champion_r"):
                sigs.append({"ticker": r["ticker"], "signal_date": r["signal_date"], "r": float(r["champion_r"])})
    hist = {}
    for fn in sorted(os.listdir(cache_dir)):
        if fn.endswith(".daily.json") and not fn.startswith("_"):
            tk = fn[:-len(".daily.json")]
            with open(os.path.join(cache_dir, fn), encoding="utf-8") as f:
                d = json.load(f)
            hist[tk] = {"dates": d["dates"], "closes": d["closes"], "highs": d["highs"]}
    index = load_history("^TWII", "daily", cache_dir, tries=1)
    if not index:
        raise SystemExit("快取裡沒有加權指數資料，無法計算相對強弱（不以假資料代替）")
    return sigs, hist, index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    sigs, hist, index = _load(a.signals, a.cache_dir)
    feats = build_features(sigs, hist, index)
    rep = {"meta": {"n_signals": len(sigs), "n_universe": len(hist)}, "split": SPLIT, **summarize(sigs, feats)}
    txt = json.dumps(rep, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
    print(txt)


if __name__ == "__main__":
    main()
