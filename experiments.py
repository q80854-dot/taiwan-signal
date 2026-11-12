"""預先登錄的 Challenger 影子實驗（2026-11-12；審查報告 T.1／E01–E08）。

原則：先定義問題、主要終點與成功條件，**再**看資料；不用回測結果反推門檻。
- 本模組只「評估」：讀影子追蹤已記錄的候選與當時的特徵，計算「若套用某個濾網，留下來的那批
  表現如何」。完全不改正式訊號、不寫任何設定、不自動升級——採用與否一律由使用者人工決定。
- 探索期（登錄日 FROZEN_AT 含以前）的資料只供參考，不得作為採用依據；因為假說本身就是看了
  這段資料才提出的，再拿同一段驗證等於自我應驗。只有登錄後才產生的資料（OOS）才計入判定。
- 門檻一律沿用系統既有的分級（非針對 10 月結果調出來的數字），登錄後凍結；要改就另立新實驗編號。
- 主要終點：Champion 分批淨 R（僅已定案 final 樣本），以「訊號日」為叢集做拔靴——同一天的
  訊號高度相關，不可當成獨立樣本。多空分開、不混池。
- 特徵缺漏的列不猜：兩組都不計入，另列 n_missing。
"""
import json
import logging
import random
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

FROZEN_AT = "2026-11-12"        # 登錄（凍結）日：此日（含）以前＝探索期
OOS_START = "2026-11-13"        # 此日（含）以後才產生的訊號＝樣本外
MIN_OOS_DAYS = 40               # 判定所需的最少 OOS 訊號日（叢集數）
MIN_OOS_TRADES = 200            # 判定所需的最少 OOS 已定案筆數（留下組）
MIN_EFFECT_R = 0.05             # 最低經濟重要差異：留下組平均 R − 基準平均 R 的 95% CI 下界須超過
N_BOOT = 2000
BOOT_SEED = 20261112


def _keep_e01(f: dict) -> Optional[bool]:
    s = f.get("sentiment")
    try:
        return None if s is None else float(s) >= 50
    except Exception:
        return None


def _keep_e02(f: dict) -> Optional[bool]:
    lv = f.get("chase_level")
    return None if lv not in ("low", "mid", "high") else lv != "high"


def _keep_e09(f: dict) -> Optional[bool]:
    d = f.get("dist_hi52")
    try:
        return None if d is None else float(d) >= -10
    except Exception:
        return None


def _keep_e10(f: dict) -> Optional[bool]:
    p = f.get("prior_blocked")
    return None if p is None else p == "none"


REGISTRY: List[Dict] = [
    {"id": "E01", "name": "弱勢市場不做多（行情濾網）", "direction": "buy",
     "hypothesis": "做多訊號在市場情緒偏弱時期望值較差；只在情緒分數 ≥ 50 時保留做多候選，"
                   "留下組的 Champion 淨 R 高於不設濾網的基準。",
     "filter": "features.sentiment >= 50",
     "threshold_source": "沿用掃描既有的情緒分級（<40 單次最多 2 檔、<50 最多 3 檔），非依 10 月結果調整",
     "keep": _keep_e01, "doc": "docs/experiments/E01_regime_gate.yaml"},
    {"id": "E02", "name": "避免追高（追高風險為『高』者不做多）", "direction": "buy",
     "hypothesis": "追高風險等級為 high（當日漲幅 ≥ 4% 或距 20 日線 > 2.5 ATR）的做多候選期望值較差；"
                   "排除後留下組的 Champion 淨 R 高於基準。",
     "filter": "features.chase_level != 'high'",
     "threshold_source": "沿用訊號引擎既有的追高分級（low／mid／high），非依 10 月結果調整",
     "keep": _keep_e02, "doc": "docs/experiments/E02_chase_filter.yaml"},
    {"id": "E09", "name": "只做接近一年高點的股票", "direction": "buy",
     "hypothesis": "收盤在近 250 日最高價九成以上的做多候選期望值較好；只保留這類，留下組的 Champion 淨 R 高於基準。",
     "filter": "features.dist_hi52 >= -10",
     "threshold_source": "研究前寫死的整數門檻（九成）；重播 2020–2026 兩期方向一致，但未通過多重比較校正，故以樣本外驗證",
     "keep": _keep_e09, "doc": "docs/experiments/E09_near_high.yaml"},
    {"id": "E10", "name": "剛轉強的不追（近 15 天內曾是被停發類別者不做）", "direction": "buy",
     "hypothesis": "同一檔股票近 15 個日曆天內曾出現被停發類別的假想單（週線不偏多／分數未達 75）者，"
                   "轉為合格後第一時間進場的期望值較差；排除後留下組的 Champion 淨 R 高於基準。",
     "filter": "features.prior_blocked == 'none'",
     "threshold_source": "回看天數＝訊號有效天數 15 天，非依結果調整；重播中這類訊號兩期皆約 −0.16R",
     "keep": _keep_e10, "doc": "docs/experiments/E10_fresh_turn.yaml"},
]


# 「被規則擋下的假想單」型實驗：不是在候選裡加濾網，而是評估「若拿掉某條擋單規則，被擋下的那批
# 表現如何」。資料來自影子追蹤的 rejected 列（features.reject＝擋單原因）。
BLOCKED_REGISTRY: List[Dict] = [
    {"id": "E03", "name": "開放大盤重挫日放空", "direction": "sell", "reject_reason": "market_stop",
     "hypothesis": "大盤重挫日（加權指數單日跌幅達熔斷門檻）目前多空皆停；其中通過全部放空關卡的假想放空單，"
                   "Champion 淨 R 的期望值為正，值得開放。",
     "filter": "kind = rejected AND features.reject = 'market_stop' AND direction = sell",
     "threshold_source": "放空關卡與平日完全相同，不新增或調整任何門檻；只評估『重挫日不發』這條規則本身",
     "min_oos_days": 8, "min_oos_trades": 30, "day_label": "重挫日", "unit_label": "假想放空單",
     "review_note": "仍須人工審查券源、軋空與尾端風險後才可提案，不自動開放。",
     "doc": "docs/experiments/E03_crash_day_short.yaml"},
    # ── 2026-11-12 Champion 變更後停發的三類訊號：「恢復觀察」。停發依據是 2020–2026 重播（兩個期間皆
    #    顯著為負）；這裡預先寫好「什麼情況下值得重新提案恢復」，之後只看 11-13 起新產生的假想單。──
    {"id": "E04", "name": "恢復放空正式訊號", "direction": "sell", "reject_reason": "short_off",
     "hypothesis": "放空已停發；其中通過全部放空關卡的假想放空單，Champion 淨 R 的期望值轉為正，值得恢復。",
     "filter": "kind = rejected AND features.reject = 'short_off' AND direction = sell",
     "threshold_source": "放空關卡與停發前完全相同，不新增或調整任何門檻",
     "min_oos_days": 40, "min_oos_trades": 200, "day_label": "訊號日", "unit_label": "假想放空單",
     "review_note": "仍須人工審查券源、軋空與尾端風險，並經使用者同意後才可恢復，不自動開放。",
     "doc": "docs/experiments/E04_restore_short.yaml"},
    {"id": "E05", "name": "恢復週線不偏多的做多", "direction": "buy", "reject_reason": "gate_weekly_long",
     "hypothesis": "週線不是偏多的做多已停發；其中通過其餘全部做多關卡的假想單，Champion 淨 R 的期望值轉為正，值得恢復。",
     "filter": "kind = rejected AND features.reject = 'gate_weekly_long' AND direction = buy",
     "threshold_source": "其餘做多關卡與停發前完全相同，不新增或調整任何門檻",
     "min_oos_days": 40, "min_oos_trades": 200, "day_label": "訊號日", "unit_label": "假想做多單",
     "review_note": "仍須人工審查回撤與尾端風險，並經使用者同意後才可恢復，不自動開放。",
     "doc": "docs/experiments/E05_restore_long_weekly.yaml"},
    {"id": "E06", "name": "恢復 65～74 分的做多", "direction": "buy", "reject_reason": "long_score",
     "hypothesis": "最終分數 65～74（週線偏多）的做多已停發；其中通過其餘全部做多關卡的假想單，Champion 淨 R 的期望值轉為正，值得恢復。",
     "filter": "kind = rejected AND features.reject = 'long_score' AND direction = buy",
     "threshold_source": "其餘做多關卡與停發前完全相同，不新增或調整任何門檻",
     "min_oos_days": 40, "min_oos_trades": 200, "day_label": "訊號日", "unit_label": "假想做多單",
     "review_note": "仍須人工審查回撤與尾端風險，並經使用者同意後才可恢復，不自動開放。",
     "doc": "docs/experiments/E06_restore_long_score.yaml"},
]


# 「出場 Challenger」型實驗：同一批做多候選、同樣的進場與成本，只換出場規則；比較逐筆成對差
# （方案淨 R − Champion 淨 R）。資料來自 shadow_signals.alt_exits（shadow.finalize_alt_exit_tracks）。
ALT_EXIT_REGISTRY: List[Dict] = [
    {"id": "E07", "name": "出場改為全程 3 ATR 移動停損（不分批、最長 60 天）", "direction": "buy", "track": "trail3",
     "hypothesis": "不分批、不設停利，全程以進場後最高價減 3 ATR 移動停損、最長持有 60 天，"
                   "逐筆淨 R 高於現行分批出場；且優勢不是只靠極少數大漲的單。",
     "filter": "kind = candidate AND direction = buy AND alt_exits.trail3.status = final AND champion_status = final",
     "threshold_source": "方案與倍數在出場研究前寫死（V5）；不依樣本外結果調整",
     "doc": "docs/experiments/E07_exit_trail3.yaml"},
    {"id": "E08", "name": "確認：到期 60 天優於原本的 15 天（分批停利不變）", "direction": "buy", "track": "exp15",
     "legacy_track": "hold60", "champion_is_treatment": True,
     "hypothesis": "分批停利與移動保本規則不變，到期 60 個日曆天的逐筆淨 R 高於到期 15 天。"
                   "2026-11-12 經使用者核准已採用 60 天；本實驗持續以原 15 天為對照，檢查這個決定在樣本外是否成立。",
     "filter": "kind = candidate AND direction = buy AND champion_status = final AND alt_exits.exp15.status = final"
               "（expire_days = 60 的列：champion − exp15；此前的列：hold60 − champion）",
     "threshold_source": "方案在出場研究前寫死（V3）；不依樣本外結果調整",
     "doc": "docs/experiments/E08_exit_hold60.yaml"},
]
WINSOR_R = 3.0          # 縮尾檢查：每筆淨 R 截在 ±3R 再比一次，確認優勢不是只靠極端值


def _alt_window(exp: Dict, rows: List[Dict], oos: bool) -> Dict:
    in_win = [r for r in rows if r.get("kind", "candidate") == "candidate" and r.get("direction") == exp["direction"]
              and ((r.get("bar_date") or "") >= OOS_START) == oos]
    pairs, n_pending = [], 0
    for r in in_win:
        if r.get("champion_status") != "final" or r.get("champion_r") is None:
            continue
        alts = r.get("alt") or {}
        # 一般情形：方案（alt）對 Champion。E08 在正式出場改為 60 天後，Champion 本身就是「方案」，
        # 對照組是原本的 15 天（exp15）；改之前的列則相反（Champion＝15 天，方案＝hold60）。
        # 兩種列都換算成同一個方向：(60 天的淨 R, 15 天的淨 R)。
        if exp.get("champion_is_treatment") and int(r.get("expire_days") or 0) == 60:
            t = alts.get(exp["track"]) or {}
            pair = (float(r["champion_r"]), t.get("r"))
        else:
            t = alts.get(exp.get("legacy_track") or exp["track"]) or {}
            pair = (t.get("r"), float(r["champion_r"]))
        if t.get("status") != "final" or t.get("r") is None:
            n_pending += 1                       # Champion 已定案、另一邊尚未出場（或無法計算）：不納入成對比較
            continue
        pairs.append((r["bar_date"], float(pair[0]), float(pair[1]), r.get("ticker") or f"_row{len(pairs)}"))
    diff, wdiff = {}, {}
    clip = lambda x: max(-WINSOR_R, min(WINSOR_R, x))
    for d, a, c, _t in pairs:
        diff.setdefault(d, []).append(a - c)
        wdiff.setdefault(d, []).append(clip(a) - clip(c))
    dv = [x for v in diff.values() for x in v]
    lo, hi = _boot_mean_ci(diff) if dv else (None, None)
    wlo, whi = _boot_mean_ci(wdiff) if dv else (None, None)
    tlo, thi = _twoway_ci([(d, t, a - c) for d, a, c, t in pairs]) if (dv and oos) else (None, None)
    alt = sorted(p[1] for p in pairs)
    out = {"window": "oos" if oos else "exploratory", "n_candidates": len(in_win), "n_pairs": len(pairs),
           "n_alt_not_final": n_pending, "n_days": len(diff),
           "treatment": "到期 60 天" if exp.get("champion_is_treatment") else exp["name"],
           "control": "到期 15 天" if exp.get("champion_is_treatment") else "現行 Champion 出場",
           "alt_avg_r": round(_mean(alt), 3) if alt else None,                       # 方案（treatment）的平均淨 R
           "champion_avg_r": round(_mean([p[2] for p in pairs]), 3) if pairs else None,   # 對照（control）的平均淨 R
           "diff": round(_mean(dv), 3) if dv else None, "diff_ci95": [lo, hi],
           "diff_winsorized": round(_mean([x for v in wdiff.values() for x in v]), 3) if dv else None,
           "diff_winsorized_ci95": [wlo, whi], "diff_ci95_twoway": [tlo, thi],
           "alt_median_r": round(alt[len(alt) // 2], 3) if alt else None,
           "alt_win_rate": round(sum(1 for a in alt if a > 0) / len(alt) * 100, 1) if alt else None}
    if not oos:
        out["verdict"] = "exploratory_only"
        out["verdict_note"] = "探索期：不得作為採用依據。"
    elif len(diff) < MIN_OOS_DAYS or len(pairs) < MIN_OOS_TRADES or lo is None or wlo is None:
        out["verdict"] = "insufficient"
        out["verdict_note"] = (f"樣本外資料不足：需 ≥ {MIN_OOS_DAYS} 個訊號日且 ≥ {MIN_OOS_TRADES} 筆成對樣本"
                               f"（目前 {len(diff)} 日、{len(pairs)} 筆）。方案最長持有 60 天，定案比 Champion 慢；不下結論。")
    elif lo > MIN_EFFECT_R and wlo > 0 and (tlo is None or tlo <= 0):
        out["verdict"] = "fragile"
        out["verdict_note"] = FRAGILE_NOTE
    elif lo > MIN_EFFECT_R and wlo > 0:
        out["verdict"] = "supported"
        out["verdict_note"] = ("成對差的 95% 區間下界超過最低經濟重要差異，且縮尾（±3R）後下界仍 > 0："
                               "優勢不是只靠極端值。"
                               + ("樣本外資料支持已採用的 60 天到期。" if exp.get("champion_is_treatment") else
                                  "仍須人工審查持有期變長對資金與同時持倉的影響，並經使用者同意，不自動採用。"))
    elif hi < MIN_EFFECT_R:
        out["verdict"] = "not_supported"
        out["verdict_note"] = ("成對差的 95% 區間上界低於最低經濟重要差異："
                               + ("樣本外資料未顯示 60 天到期優於 15 天，應向使用者提出是否改回。"
                                  if exp.get("champion_is_treatment") else "此出場方案未顯示實用改善。"))
    elif lo > MIN_EFFECT_R:
        out["verdict"] = "tail_dependent"
        out["verdict_note"] = ("平均成對差顯著為正，但縮尾（±3R）後不成立：優勢集中在極少數大漲的單，"
                               "實際只做其中少數幾筆時很可能拿不到。不建議採用。")
    else:
        out["verdict"] = "inconclusive"
        out["verdict_note"] = "95% 區間跨越最低經濟重要差異：尚無法判定，繼續累積。"
    return out


def evaluate_alt_exit(exp_id: str, rows: Optional[List[Dict]] = None) -> Dict:
    exp = next((e for e in ALT_EXIT_REGISTRY if e["id"] == exp_id), None)
    if exp is None:
        return {"ok": False, "reason": "unknown_experiment", "id": exp_id}
    rows = _load_rows() if rows is None else rows
    return {"ok": True, "type": "alt_exit", **exp,
            "exploratory": _alt_window(exp, rows, oos=False), "oos": _alt_window(exp, rows, oos=True)}


def _twoway_ci(obs: List[tuple], kept_mask: Optional[List[bool]] = None, n_boot=N_BOOT, seed=BOOT_SEED):
    """★ 2026-11-12（S-P1-05）二維拔靴：訊號日 × 股票 各自有放回重抽（pigeonhole bootstrap），每筆權重＝
    該日被抽中次數 × 該股票被抽中次數。同一天的訊號互相連動、同一檔股票反覆出現，兩種相關同時考慮。
    obs：[(日期, 股票, 值)]。kept_mask 為 None 時回傳「加權平均」的 95% 區間；否則回傳
    「留下組加權平均 − 全部加權平均」（兩者同一組權重，成對比較）。日期或股票少於 5 個回 (None, None)。
    7 年重播驗證（docs/research/MATCHED_AND_BOOTSTRAP_2020_2026.md）：只以訊號日分群的區間偏窄，二維區間寬約 1.5～1.9 倍。"""
    if not obs:
        return None, None
    try:
        import numpy as np
    except Exception:
        return None, None
    days = sorted({o[0] for o in obs}); ticks = sorted({o[1] for o in obs})
    if len(days) < 5 or len(ticks) < 5:
        return None, None
    di = {d: i for i, d in enumerate(days)}; ti = {t: i for i, t in enumerate(ticks)}
    ia = np.array([di[o[0]] for o in obs]); it = np.array([ti[o[1]] for o in obs])
    v = np.array([float(o[2]) for o in obs])
    km = None if kept_mask is None else np.array(kept_mask, bool)
    rng = np.random.default_rng(seed); stats = []
    for _ in range(n_boot):
        wa = np.bincount(rng.integers(0, len(days), len(days)), minlength=len(days))
        wt = np.bincount(rng.integers(0, len(ticks), len(ticks)), minlength=len(ticks))
        w = wa[ia] * wt[it]
        sw = w.sum()
        if not sw:
            continue
        if km is None:
            stats.append(float((w * v).sum() / sw))
        else:
            swk = w[km].sum()
            if swk:
                stats.append(float((w[km] * v[km]).sum() / swk - (w * v).sum() / sw))
    if len(stats) < 100:
        return None, None
    stats.sort()
    return round(stats[int(0.025 * len(stats))], 3), round(stats[min(len(stats) - 1, int(0.975 * len(stats)))], 3)


def _tk(r, i):
    """二維拔靴用的股票鍵；缺代號的列各自成一群（不把不明列合併成同一檔）。"""
    return r.get("ticker") or f"_row{i}"


FRAGILE_NOTE = ("以訊號日分群時成立，但同時考慮「同一檔股票反覆出現」後（二維拔靴）95% 區間含 0："
                "證據不夠穩，不提案，繼續累積。")


def _boot_mean_ci(by_day: Dict[str, list], n_boot=N_BOOT, seed=BOOT_SEED):
    days = sorted(by_day)
    if len(days) < 5:
        return None, None
    rng = random.Random(seed); means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(days)):
            pool.extend(by_day[days[rng.randrange(len(days))]])
        if pool:
            means.append(sum(pool) / len(pool))
    if len(means) < 100:
        return None, None
    means.sort()
    return round(means[int(0.025 * len(means))], 3), round(means[min(len(means) - 1, int(0.975 * len(means)))], 3)


def _blocked_window(exp: Dict, rows: List[Dict], oos: bool) -> Dict:
    in_win = [r for r in rows if r.get("kind") == "rejected" and r.get("direction") == exp["direction"]
              and r["f"].get("reject") == exp["reject_reason"]
              and ((r.get("bar_date") or "") >= OOS_START) == oos]
    final = [r for r in in_win if r.get("champion_status") == "final" and r.get("champion_r") is not None]
    by_day = {}
    for r in final:
        by_day.setdefault(r["bar_date"], []).append(r["champion_r"])
    vals = [v for vs in by_day.values() for v in vs]
    lo, hi = _boot_mean_ci(by_day) if vals else (None, None)
    tlo, thi = (_twoway_ci([(r["bar_date"], _tk(r, i), r["champion_r"]) for i, r in enumerate(final)])
                if (vals and oos) else (None, None))
    out = {"window": "oos" if oos else "exploratory", "n_blocked": len(in_win), "n_final": len(final),
           "not_final_share": round(1 - len(final) / len(in_win), 3) if in_win else None,
           "n_days": len(by_day), "avg_r": round(_mean(vals), 3) if vals else None, "avg_r_ci95": [lo, hi],
           "avg_r_ci95_twoway": [tlo, thi],
           "net_positive_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None}
    if not oos:
        out["verdict"] = "exploratory_only"
        out["verdict_note"] = "探索期：不得作為採用依據。"
    elif len(by_day) < exp["min_oos_days"] or len(vals) < exp["min_oos_trades"] or lo is None:
        out["verdict"] = "insufficient"
        _dl = exp.get("day_label", "訊號日")
        out["verdict_note"] = (f"樣本外資料不足：需 ≥ {exp['min_oos_days']} 個{_dl}且 ≥ {exp['min_oos_trades']} 筆已定案"
                               f"（目前 {len(by_day)} 日、{len(vals)} 筆）。"
                               + ("重挫日本來就少，累積會很慢；" if _dl == "重挫日" else "") + "不下結論。")
    elif lo > MIN_EFFECT_R and (tlo is None or tlo <= 0):
        out["verdict"] = "fragile"
        out["verdict_note"] = FRAGILE_NOTE
    elif lo > MIN_EFFECT_R:
        out["verdict"] = "supported"
        out["verdict_note"] = (f"被擋下的{exp.get('unit_label', '假想單')}平均淨 R 的 95% 區間下界超過最低經濟重要差異。"
                               + exp.get("review_note", "仍須人工審查後才可提案，不自動開放。"))
    elif hi < MIN_EFFECT_R:
        out["verdict"] = "not_supported"
        out["verdict_note"] = f"平均淨 R 的 95% 區間上界低於最低經濟重要差異：「{exp['name']}」未顯示實用價值，維持現狀。"
    else:
        out["verdict"] = "inconclusive"
        out["verdict_note"] = "95% 區間跨越最低經濟重要差異：尚無法判定，繼續累積。"
    return out


def evaluate_blocked(exp_id: str, rows: Optional[List[Dict]] = None) -> Dict:
    exp = next((e for e in BLOCKED_REGISTRY if e["id"] == exp_id), None)
    if exp is None:
        return {"ok": False, "reason": "unknown_experiment", "id": exp_id}
    rows = _load_rows() if rows is None else rows
    return {"ok": True, "type": "blocked_rule", **exp,
            "exploratory": _blocked_window(exp, rows, oos=False), "oos": _blocked_window(exp, rows, oos=True)}


def _load_rows() -> List[Dict]:
    from state_store import store
    import shadow
    shadow.ensure_table()
    with store._conn() as conn:
        rs = [dict(r) for r in conn.execute(
            "SELECT bar_date, ticker, kind, direction, status, champion_r, champion_status, features, alt_exits, expire_days "
            "FROM shadow_signals WHERE kind IN ('candidate','rejected') ORDER BY bar_date").fetchall()]
    for r in rs:
        try:
            f = json.loads(r.get("features") or "{}")
            r["f"] = f if isinstance(f, dict) else {}
        except Exception:
            r["f"] = {}
        try:
            a = json.loads(r.get("alt_exits") or "{}")
            r["alt"] = a if isinstance(a, dict) else {}
        except Exception:
            r["alt"] = {}
    return rs


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _boot_diff_ci(kept: Dict[str, list], base: Dict[str, list], n_boot=N_BOOT, seed=BOOT_SEED):
    """以訊號日為叢集重抽，回傳「留下組平均 − 基準平均」的 (lo95, hi95)；叢集不足回 (None, None)。
    同一次重抽中兩組用同一批日期，保留當日共同波動（成對比較）。"""
    days = sorted(base)
    if len(days) < 5:
        return None, None
    rng = random.Random(seed); diffs = []
    for _ in range(n_boot):
        pk, pb = [], []
        for _ in range(len(days)):
            d = days[rng.randrange(len(days))]
            pk.extend(kept.get(d, [])); pb.extend(base[d])
        if pk and pb:
            diffs.append(sum(pk) / len(pk) - sum(pb) / len(pb))
    if len(diffs) < 100:
        return None, None
    diffs.sort()
    return round(diffs[int(0.025 * len(diffs))], 3), round(diffs[min(len(diffs) - 1, int(0.975 * len(diffs)))], 3)


def _window(exp: Dict, rows: List[Dict], oos: bool) -> Dict:
    in_win = [r for r in rows if r.get("direction") == exp["direction"]
              and r.get("kind", "candidate") == "candidate"
              and ((r.get("bar_date") or "") >= OOS_START) == oos]
    final = [r for r in in_win if r.get("champion_status") == "final" and r.get("champion_r") is not None]
    kept, base, n_missing, n_dropped = {}, {}, 0, 0
    tw_obs, tw_mask = [], []
    for i, r in enumerate(final):
        k = exp["keep"](r["f"])
        if k is None:
            n_missing += 1                      # 特徵缺漏：兩組都不計，不猜
            continue
        tw_obs.append((r["bar_date"], _tk(r, i), r["champion_r"])); tw_mask.append(bool(k))
        base.setdefault(r["bar_date"], []).append(r["champion_r"])
        if k:
            kept.setdefault(r["bar_date"], []).append(r["champion_r"])
        else:
            n_dropped += 1
    kv = [v for vs in kept.values() for v in vs]; bv = [v for vs in base.values() for v in vs]
    dv = None
    if kv and bv:
        dv = round(_mean(kv) - _mean(bv), 3)
    lo, hi = _boot_diff_ci(kept, base) if (kv and bv) else (None, None)
    tlo, thi = _twoway_ci(tw_obs, tw_mask) if (kv and bv and oos) else (None, None)
    out = {"window": "oos" if oos else "exploratory",
           "n_candidates": len(in_win), "n_final": len(final),
           "not_final_share": round(1 - len(final) / len(in_win), 3) if in_win else None,
           "n_missing_feature": n_missing, "n_baseline": len(bv), "n_kept": len(kv), "n_dropped": n_dropped,
           "trade_retention": round(len(kv) / len(bv), 3) if bv else None,
           "n_days_baseline": len(base), "n_days_kept": len(kept),
           "baseline_avg_r": round(_mean(bv), 3) if bv else None,
           "kept_avg_r": round(_mean(kv), 3) if kv else None,
           "diff_kept_minus_baseline": dv, "diff_ci95": [lo, hi], "diff_ci95_twoway": [tlo, thi]}
    if not oos:
        out["verdict"] = "exploratory_only"
        out["verdict_note"] = "探索期：假說由這段資料提出，不得用同一段資料作為採用依據。"
    elif len(base) < MIN_OOS_DAYS or len(kv) < MIN_OOS_TRADES or lo is None:
        out["verdict"] = "insufficient"
        out["verdict_note"] = (f"樣本外資料不足：需 ≥ {MIN_OOS_DAYS} 個訊號日且留下組 ≥ {MIN_OOS_TRADES} 筆已定案"
                               f"（目前 {len(base)} 日、{len(kv)} 筆）。繼續累積，不下結論。")
    elif lo > MIN_EFFECT_R and (tlo is None or tlo <= 0):
        out["verdict"] = "fragile"
        out["verdict_note"] = FRAGILE_NOTE
    elif lo > MIN_EFFECT_R:
        out["verdict"] = "supported"
        out["verdict_note"] = ("樣本外差異的 95% 區間下界超過最低經濟重要差異，且二維拔靴仍為正。"
                               "仍須人工審查回撤、可成交性後才可提案，不自動採用。")
    elif hi < MIN_EFFECT_R:
        out["verdict"] = "not_supported"
        out["verdict_note"] = "樣本外差異的 95% 區間上界低於最低經濟重要差異：此濾網未顯示實用改善。"
    else:
        out["verdict"] = "inconclusive"
        out["verdict_note"] = "樣本外差異的 95% 區間跨越最低經濟重要差異：尚無法判定，繼續累積。"
    return out


def evaluate(exp_id: str, rows: Optional[List[Dict]] = None) -> Dict:
    exp = next((e for e in REGISTRY if e["id"] == exp_id), None)
    if exp is None:
        return {"ok": False, "reason": "unknown_experiment", "id": exp_id}
    rows = _load_rows() if rows is None else rows
    meta = {k: v for k, v in exp.items() if k != "keep"}
    return {"ok": True, **meta, "exploratory": _window(exp, rows, oos=False), "oos": _window(exp, rows, oos=True)}


def report() -> Dict:
    rows = _load_rows()
    return {"ok": True, "frozen_at": FROZEN_AT, "oos_start": OOS_START,
            "primary_endpoint": "Champion 分批淨 R（僅已定案樣本）：留下組平均 − 基準平均，訊號日叢集拔靴 95% CI",
            "decision_rule": {"min_oos_days": MIN_OOS_DAYS, "min_oos_trades_kept": MIN_OOS_TRADES,
                              "min_effect_r": MIN_EFFECT_R,
                              "supported": "OOS 差異 CI 下界 > min_effect_r",
                              "not_supported": "OOS 差異 CI 上界 < min_effect_r",
                              "fragile": "日叢集 CI 下界 > min_effect_r，但二維拔靴（訊號日 × 股票）CI 下界 ≤ 0",
                              "robustness": "supported 須同時通過二維拔靴（2026-11-12 加入，早於任何樣本外資料）",
                              "family_size": len(REGISTRY) + len(BLOCKED_REGISTRY) + len(ALT_EXIT_REGISTRY)},
            "experiments": [evaluate(e["id"], rows) for e in REGISTRY]
                           + [evaluate_blocked(e["id"], rows) for e in BLOCKED_REGISTRY]
                           + [evaluate_alt_exit(e["id"], rows) for e in ALT_EXIT_REGISTRY],
            "note": "純影子評估：不改正式訊號、不寫設定、不自動升級。supported 也只代表『可以提案』，"
                    "仍須人工審查回撤、尾端風險與可成交性，並經使用者同意。同時評估多個實驗有多重比較問題，"
                    "新增實驗須另立編號並更新 family_size。"}
