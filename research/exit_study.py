"""出場方式研究（研究工具；不被 app 匯入、不影響任何線上行為）。

問題：重播顯示約三分之一的交易在 15 個日曆天到期時還在半途。延長持有、改用移動停損、或提早
砍掉沒動靜的單，能不能改善每筆期望值？

做法：取 champion_replay.py 產出的訊號（進場價、停損、停利皆為訊號當時的值，不重算），對同一批
訊號逐筆以不同出場規則重新模擬。所有方案共用正式的分批出場狀態機（execution.StagedPosition）
與影子同口徑的成本——只改「到期天數／是否移動停損／是否保留停利階梯／時間停損」，其餘慣例
完全相同：同一根 K 棒停損優先於停利、跳空穿越停損以開盤價成交、一價鎖死的漲跌停順延、
無效 K 棒略過。移動停損只用「到前一根為止」的最高（低）價，當根才生效，沒有未來函數。

方案在看結果之前就寫死在 VARIANTS（共 8 個，含基準），不依結果增刪。判定方式也事前寫死：
  1. 只在 2020–2023（訓練期）挑：與基準的『逐筆成對差』平均最高、且其 95% 區間下界 > 0 者。
  2. 到 2024–2026（驗證期）確認：同一方案的成對差 95% 區間下界仍 > 0 才算成立。
  3. 區間一律以「訊號日」為叢集做拔靴——同一天的訊號高度相關，不能當獨立樣本。
  4. 一次比 7 個方案有多重比較問題：訓練期用 Bonferroni（99.3% 區間）挑，驗證期只驗被挑中的那一個。

用法：python research/exit_study.py --signals replay.csv --cache-dir hist/ [--out exit_study.json]
"""
import argparse
import csv
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SPLIT = "2024-01-01"            # 訓練期 < SPLIT ≤ 驗證期
BARS_AFTER = 75                 # 最長方案 60 個日曆天 ≈ 42 根；多留一些給順延
SHARES = 3000
N_BOOT = 2000
SEED = 20261112

# 事前寫死的方案。expire＝到期日曆天；trail_atr＝吊燈式移動停損的 ATR 倍數（None＝不用）；
# trail_after＝"entry"（進場就開始）或 "tp1"（TP1 成交後才開始）；ladder＝是否保留 TP1/TP2/TP3 分批；
# drop_tp3＝最後 1/3 不設停利、交給移動停損；time_stop＝(日曆天, 條件) 到該天仍未觸及 TP1 就以收盤出場。
VARIANTS = [
    {"id": "V0", "name": "現行（分批停利、15 天到期）", "expire": 15, "ladder": True},
    {"id": "V1", "name": "到期延長為 30 天", "expire": 30, "ladder": True},
    {"id": "V2", "name": "到期延長為 45 天", "expire": 45, "ladder": True},
    {"id": "V3", "name": "到期延長為 60 天", "expire": 60, "ladder": True},
    {"id": "V4", "name": "TP1 後改 3 ATR 移動停損、最後 1/3 不設停利、60 天到期",
     "expire": 60, "ladder": True, "drop_tp3": True, "trail_atr": 3.0, "trail_after": "tp1"},
    {"id": "V5", "name": "不分批：全程 3 ATR 移動停損、60 天到期",
     "expire": 60, "ladder": False, "trail_atr": 3.0, "trail_after": "entry"},
    {"id": "V6", "name": "不分批：全程 2 ATR 移動停損、60 天到期",
     "expire": 60, "ladder": False, "trail_atr": 2.0, "trail_after": "entry"},
    {"id": "V7", "name": "現行＋時間停損（7 天內未到 TP1 即以收盤出場）",
     "expire": 15, "ladder": True, "time_stop": 7},
]


def _position_cls():
    import execution

    class FlexPosition(execution.StagedPosition):
        """在正式狀態機之上加：移動停損、時間停損。其餘成交慣例完全沿用父類別。"""

        def configure(self, trail_atr=None, atr=None, trail_after="entry", time_stop=None):
            self.trail_atr, self.atr, self.trail_after, self.time_stop = trail_atr, atr, trail_after, time_stop
            self.extreme = None
            return self

        def on_bar(self, b):
            # 一價鎖死（賣不掉／補不回）要在呼叫父類別之前判定——父類別會更新 prev_close
            locked = execution.bar_ok(b) and execution.exit_blocked_by_limit(self.direction, b, self.prev_close)
            legs = super().on_bar(b)
            if self.remaining <= 0 or not execution.bar_ok(b) or locked:
                return legs
            bd = b["bar_date"]
            # 時間停損：到指定日曆天、仍未成交任何一段停利 → 以本根收盤出場（鎖死的那根已在上面順延）
            if self.time_stop and not self.done["tp1"] and self.days(bd) >= self.time_stop:
                leg = self.force_close(b, stage="time_stop")
                return legs + ([leg] if leg else [])
            # 移動停損：以本根（含）為止的極值更新，下一根才生效
            if self.trail_atr and self.atr and (self.trail_after == "entry" or self.done["tp1"]):
                if self.buy:
                    self.extreme = b["high"] if self.extreme is None else max(self.extreme, b["high"])
                    self.current_sl = max(self.current_sl, self.extreme - self.trail_atr * self.atr)
                else:
                    self.extreme = b["low"] if self.extreme is None else min(self.extreme, b["low"])
                    self.current_sl = min(self.current_sl, self.extreme + self.trail_atr * self.atr)
            return legs

    return FlexPosition


def simulate(v, sig, bars, pnl_fn, cls):
    """回傳 {r, exit_date, hold_days, open_end} 或 None。sig 為重播 CSV 的一列（已轉型）。"""
    import execution
    entry, sl, risk = sig["fill"], sig["stop"], abs(sig["entry_plan"] - sig["stop"])
    if not entry or risk <= 0 or not bars:
        return None
    ladder = v.get("ladder", True)
    tp1 = sig["tp1"] if ladder else None
    tp2 = sig["tp2"] if ladder else None
    tp3 = sig["tp3"] if (ladder and not v.get("drop_tp3")) else None
    pos = cls(entry, sl, tp1, tp2, tp3, sig["direction"], SHARES, sig["signal_date"], pnl_fn,
              v["expire"], prev_close=sig["prev_close"])
    atr = risk / sig["sl_atr"] if sig.get("sl_atr") else None
    pos.configure(v.get("trail_atr"), atr, v.get("trail_after", "entry"), v.get("time_stop"))
    if v.get("trail_atr") and not atr:
        return None
    for b in bars:
        if pos.remaining <= 0:
            break
        pos.on_bar(b)
    open_end = pos.remaining > 0            # 資料用盡仍未出場：不納入（不以暫估值當結果）
    if open_end or not pos.legs:
        return {"open_end": True}
    pnl = sum(l["pnl"] for l in pos.legs)
    hold = (datetime.strptime(pos.exit_date, "%Y-%m-%d") - datetime.strptime(sig["signal_date"], "%Y-%m-%d")).days
    return {"r": pnl / (risk * SHARES), "exit_date": pos.exit_date, "hold_days": hold, "open_end": False,
            "last": pos.legs[-1]["stage"]}


def _boot(by_day, n_boot=N_BOOT, seed=SEED, alpha=0.05):
    """以訊號日為叢集的拔靴：回傳 (平均, 下界, 上界)。by_day: {日期: [值…]}。"""
    days = sorted(by_day)
    vals = [x for d in days for x in by_day[d]]
    if not vals:
        return None, None, None
    mean = sum(vals) / len(vals)
    if len(days) < 20:
        return mean, None, None
    rng = random.Random(seed)
    ms = []
    for _ in range(n_boot):
        s = n = 0
        for _ in range(len(days)):
            xs = by_day[days[rng.randrange(len(days))]]
            s += sum(xs)
            n += len(xs)
        if n:
            ms.append(s / n)
    ms.sort()
    return mean, ms[int(alpha / 2 * len(ms))], ms[min(len(ms) - 1, int((1 - alpha / 2) * len(ms)))]


def _load_signals(path, direction="buy"):
    out = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("direction") != direction or r.get("status") not in ("final", "provisional"):
                continue
            try:
                out.append({"ticker": r["ticker"], "signal_date": r["signal_date"], "direction": r["direction"],
                            "fill_date": r["fill_date"], "fill": float(r["fill"]),
                            "entry_plan": float(r["entry_plan"]), "stop": float(r["stop"]),
                            "tp1": float(r["tp1"]), "tp2": float(r["tp2"]), "tp3": float(r["tp3"]),
                            "sl_atr": float(r["sl_atr"]) if r.get("sl_atr") else None,
                            "score": float(r["score"]), "csv_r": float(r["champion_r"]) if r.get("champion_r") else None,
                            "csv_status": r["status"]})
            except (ValueError, KeyError):
                continue
    return out


def run(signals_path, cache_dir, direction="buy"):
    import shadow
    from research.champion_replay import load_history
    cls, pnl_fn = _position_cls(), shadow._shadow_staged_pnl_factory()
    sigs = _load_signals(signals_path, direction)
    by_ticker = defaultdict(list)
    for s in sigs:
        by_ticker[s["ticker"]].append(s)
    res = {v["id"]: [] for v in VARIANTS}          # 每個方案：[(signal_date, ticker, r, hold, exit_date)]
    n_nodata = n_open = 0
    mismatch = []
    for tk, lst in by_ticker.items():
        d = load_history(tk, "daily", cache_dir, tries=1)
        if not d:
            n_nodata += len(lst)
            continue
        idx = {dt: i for i, dt in enumerate(d["dates"])}
        for s in sorted(lst, key=lambda x: x["signal_date"]):
            i = idx.get(s["fill_date"])
            if i is None or i == 0:
                n_nodata += 1
                continue
            s["prev_close"] = d["closes"][i - 1]
            bars = [{"bar_date": d["dates"][j], "open": d["opens"][j], "high": d["highs"][j], "low": d["lows"][j],
                     "close": d["closes"][j]} for j in range(i, min(len(d["closes"]), i + BARS_AFTER))]
            row = {}
            for v in VARIANTS:
                o = simulate(v, s, bars, pnl_fn, cls)
                if not o or o.get("open_end"):
                    row = None
                    break
                row[v["id"]] = o
            if row is None:                         # 任何一個方案沒走完（資料尾端）→ 整筆不納入，確保成對比較
                n_open += 1
                continue
            if s["csv_status"] == "final" and s["csv_r"] is not None and abs(row["V0"]["r"] - s["csv_r"]) > 0.002:
                mismatch.append((tk, s["signal_date"], s["csv_r"], round(row["V0"]["r"], 3)))
            for vid, o in row.items():
                res[vid].append((s["signal_date"], tk, o["r"], o["hold_days"], o["exit_date"], o["last"]))
    return res, {"n_signals": len(sigs), "n_nodata": n_nodata, "n_open_at_data_end": n_open,
                 "n_used": len(res["V0"]), "baseline_mismatch": len(mismatch), "mismatch_examples": mismatch[:5]}


def _no_overlap_total(rows):
    """同一檔在前一筆出場前不再進場（持有愈久、能接的訊號愈少）：回傳 (筆數, 總 R)。"""
    busy, n, tot = {}, 0, 0.0
    for sd, tk, r, _h, ex, _l in sorted(rows):
        if sd <= busy.get(tk, ""):
            continue
        busy[tk] = ex
        n += 1
        tot += r
    return n, tot


def summarize(res):
    out = {}
    n_cmp = len(VARIANTS) - 1
    base = {(sd, tk): r for sd, tk, r, *_ in res["V0"]}
    for v in VARIANTS:
        rows = res[v["id"]]
        o = {"name": v["name"]}
        for label, keep in (("train", lambda sd: sd < SPLIT), ("test", lambda sd: sd >= SPLIT)):
            rs = [x for x in rows if keep(x[0])]
            by_day, diff_day = defaultdict(list), defaultdict(list)
            for sd, tk, r, *_ in rs:
                by_day[sd].append(r)
                diff_day[sd].append(r - base[(sd, tk)])
            m, lo, hi = _boot(by_day)
            dm, dlo, dhi = _boot(diff_day)
            _, blo, bhi = _boot(diff_day, alpha=0.05 / n_cmp)        # Bonferroni：訓練期挑選用
            n_no, tot_no = _no_overlap_total(rs)
            holds = sorted(x[3] for x in rs)
            o[label] = {
                "n": len(rs), "n_days": len(by_day),
                "avg_r": round(m, 4) if m is not None else None,
                "avg_r_ci95": [round(lo, 4), round(hi, 4)] if lo is not None else None,
                "win_rate": round(sum(1 for x in rs if x[2] > 0) / len(rs) * 100, 1) if rs else None,
                "diff_vs_base": round(dm, 4) if dm is not None else None,
                "diff_ci95": [round(dlo, 4), round(dhi, 4)] if dlo is not None else None,
                "diff_ci_bonferroni": [round(blo, 4), round(bhi, 4)] if blo is not None else None,
                "median_hold_days": holds[len(holds) // 2] if holds else None,
                "avg_hold_days": round(sum(holds) / len(holds), 1) if holds else None,
                "expired_share": round(sum(1 for x in rs if x[5] in ("expired", "time_stop")) / len(rs) * 100, 1) if rs else None,
                "no_overlap_n": n_no, "no_overlap_total_r": round(tot_no, 1),
                "no_overlap_avg_r": round(tot_no / n_no, 4) if n_no else None,
            }
        out[v["id"]] = o
    # 事前寫死的判定
    cand = [(out[v["id"]]["train"]["diff_vs_base"], v["id"]) for v in VARIANTS[1:]
            if out[v["id"]]["train"]["diff_ci_bonferroni"] and out[v["id"]]["train"]["diff_ci_bonferroni"][0] > 0]
    verdict = {"rule": "訓練期成對差的 Bonferroni 區間下界 > 0 者中取平均最高；驗證期成對差 95% 區間下界 > 0 才成立"}
    if not cand:
        verdict.update(selected=None, result="none_selected",
                       note="訓練期沒有任何方案顯著優於現行出場；不進入驗證，不建議更改出場方式。")
    else:
        best = max(cand)[1]
        t = out[best]["test"]
        ok = bool(t["diff_ci95"] and t["diff_ci95"][0] > 0)
        verdict.update(selected=best, result="confirmed" if ok else "not_confirmed",
                       note=(f"{best} 在驗證期的成對差 {t['diff_vs_base']}R、95% 區間 {t['diff_ci95']}："
                             + ("樣本外仍優於現行出場。" if ok else "樣本外未能確認，不建議更改。")))
    return {"variants": out, "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--direction", default="buy")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res, meta = run(a.signals, a.cache_dir, a.direction)
    rep = {"meta": meta, "split": SPLIT, **summarize(res)}
    txt = json.dumps(rep, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
    print(txt)


if __name__ == "__main__":
    main()
