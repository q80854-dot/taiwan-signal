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
]


def _load_rows() -> List[Dict]:
    from state_store import store
    import shadow
    shadow.ensure_table()
    with store._conn() as conn:
        rs = [dict(r) for r in conn.execute(
            "SELECT bar_date, direction, status, champion_r, champion_status, features "
            "FROM shadow_signals WHERE kind='candidate' ORDER BY bar_date").fetchall()]
    for r in rs:
        try:
            f = json.loads(r.get("features") or "{}")
            r["f"] = f if isinstance(f, dict) else {}
        except Exception:
            r["f"] = {}
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
              and ((r.get("bar_date") or "") >= OOS_START) == oos]
    final = [r for r in in_win if r.get("champion_status") == "final" and r.get("champion_r") is not None]
    kept, base, n_missing, n_dropped = {}, {}, 0, 0
    for r in final:
        k = exp["keep"](r["f"])
        if k is None:
            n_missing += 1                      # 特徵缺漏：兩組都不計，不猜
            continue
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
    out = {"window": "oos" if oos else "exploratory",
           "n_candidates": len(in_win), "n_final": len(final),
           "not_final_share": round(1 - len(final) / len(in_win), 3) if in_win else None,
           "n_missing_feature": n_missing, "n_baseline": len(bv), "n_kept": len(kv), "n_dropped": n_dropped,
           "trade_retention": round(len(kv) / len(bv), 3) if bv else None,
           "n_days_baseline": len(base), "n_days_kept": len(kept),
           "baseline_avg_r": round(_mean(bv), 3) if bv else None,
           "kept_avg_r": round(_mean(kv), 3) if kv else None,
           "diff_kept_minus_baseline": dv, "diff_ci95": [lo, hi]}
    if not oos:
        out["verdict"] = "exploratory_only"
        out["verdict_note"] = "探索期：假說由這段資料提出，不得用同一段資料作為採用依據。"
    elif len(base) < MIN_OOS_DAYS or len(kv) < MIN_OOS_TRADES or lo is None:
        out["verdict"] = "insufficient"
        out["verdict_note"] = (f"樣本外資料不足：需 ≥ {MIN_OOS_DAYS} 個訊號日且留下組 ≥ {MIN_OOS_TRADES} 筆已定案"
                               f"（目前 {len(base)} 日、{len(kv)} 筆）。繼續累積，不下結論。")
    elif lo > MIN_EFFECT_R:
        out["verdict"] = "supported"
        out["verdict_note"] = "樣本外差異的 95% 區間下界超過最低經濟重要差異。仍須人工審查回撤、可成交性後才可提案，不自動採用。"
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
                              "family_size": len(REGISTRY)},
            "experiments": [evaluate(e["id"], rows) for e in REGISTRY],
            "note": "純影子評估：不改正式訊號、不寫設定、不自動升級。supported 也只代表『可以提案』，"
                    "仍須人工審查回撤、尾端風險與可成交性，並經使用者同意。同時評估多個實驗有多重比較問題，"
                    "新增實驗須另立編號並更新 family_size。"}
