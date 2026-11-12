"""策略學習・暖啟動勝率模型（第一版，離線訓練、shadow-only）。

使用者要「像 AI 一樣不斷用資料模擬、從既有規則基礎(50-60%)再學」。這支用已累積的
shadow_signals（每筆訊號的特徵 JSON ＋ 實算淨 R）訓練一個「勝率」模型：

- 標籤 y = 1 若 r_multiple > 0（勝），否則 0。用系統已算好的淨 R，不另定義、不編造。
- 特徵 = 訊號產生當下擷取的 features（純數值、覆蓋率≥門檻者）＋ 規則分數 score
  （把現有規則當成其中一個特徵＝暖啟動/先驗，不是從 0 學起）。
- 模型 = numpy 邏輯回歸（可解釋、輕量，不引入 sklearn 等重依賴，守住 Render 記憶體）。
- 評估 =（★ 2026-11-04 F-06 修正）時間序列 walk-forward（expanding window）交叉驗證：
  嚴格「只用較早樣本訓練、對較晚樣本預測」，且每個 fold 的補值、特徵選擇、標準化
  都只在該 fold 的訓練段 fit、驗證段只 transform；杜絕先前「隨機打亂 + 在全資料上
  先做前處理再切 fold」造成的未來資料洩漏（那會讓 AUC 系統性偏樂觀）。同時和「只用
  規則分數」的基準在同一批驗證樣本上比較是否真有提升，並附 Brier 分數與十分位勝率/平均 R。
- 嚴格守「不要資訊錯誤」：資料不足、分母異常、數值不合理一律回報 insufficient，不硬給結論；
  本模組只訓練、評估、把模型係數存進 meta，**完全不改動即時訊號**（之後要用也先 shadow 並行）。
"""
import json
import logging
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

META_KEY = "learning_model_v1"
MIN_SAMPLES = 400          # 低於此樣本數不訓練（避免過擬合、結論不穩）
MIN_COVERAGE = 0.6         # 特徵在樣本中出現比例低於此者不納入
# 明顯非預測特徵或會造成雜訊的鍵（文字類、識別類），排除
# ★ 2026-11-04：排除 month。本系統目前僅累積約 5 週資料，month 只取 2~3 個值、幾乎與時間軸
# 一一對應，模型會「背時間窗口」而非學規律——這是誠實評估（walk-forward 0.66 → 嚴格 holdout
# 0.41）落差的過擬合來源之一。weekday(dow) 每週重複、不是時間軸代理，暫予保留。
# 日後若累積到跨多年資料、month 才可能代表真正的季節性，再評估是否放回。
_EXCLUDE_KEYS = {"reject", "sector", "code", "ticker", "name", "direction", "kind", "month"}


def _rows(direction="buy"):
    """讀已結算、有淨 R 的 shadow 列：score / r_multiple / kind / features。
    ★ F-06：依 bar_date（訊號決策日）＋ created_at 由舊到新排序，提供時間序列評估所需的
    時間順序；排序在 SQL 完成，build_dataset 僅保留此順序。
    ★ 2026-11-08（多空標籤拆分／狀態分離）：只取 kind='candidate'（策略實際候選，不混
    rejected/control——它們的選擇機制不同，混入會污染標籤），且依 direction 分開（多方與空方
    報酬分布、動態相反，不可共用同一模型）。預設做多；空方樣本目前太少、不足以單獨建模。
    direction=None 時回全部方向（僅供研究檢視，不建議用於訓練）。"""
    from state_store import store
    import shadow
    shadow.ensure_table()
    where = "WHERE status='closed' AND r_multiple IS NOT NULL AND kind='candidate'"
    params = []
    if direction:
        where += " AND direction=?"
        params.append(direction)
    with store._conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT bar_date, exit_date, score, r_multiple, kind, direction, features FROM shadow_signals "
            + where + " ORDER BY bar_date ASC, created_at ASC", tuple(params)).fetchall()]


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except Exception:
        return None


def build_dataset(direction="buy"):
    """回傳 {ok, parsed, y, score, rmult, n, direction} 或 {ok:False,...}（資料不足）。
    parsed：依時間由舊到新的 [(特徵字典, 規則分數), ...]（未填補、未標準化——前處理延後到
    各 fold 的訓練段才 fit，避免洩漏）；y：勝(1)/負(0)；score：規則分數；rmult：淨 R。
    ★ 2026-11-08：只用單一方向的候選（預設 buy），多空不混。"""
    rows = _rows(direction)
    if len(rows) < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient", "n": len(rows), "direction": direction}
    parsed, ys, scores, rmults, bar_dates, end_dates, end_known = [], [], [], [], [], [], []
    for r in rows:
        rm = _num(r.get("r_multiple"))
        if rm is None:
            continue
        try:
            feats = json.loads(r.get("features") or "{}")
        except Exception:
            feats = {}
        if not isinstance(feats, dict):
            feats = {}
        fnum = {}
        for k, v in feats.items():
            if k in _EXCLUDE_KEYS:
                continue
            nv = _num(v)
            if nv is not None:
                fnum[k] = nv
        sc = _num(r.get("score"))
        parsed.append((fnum, sc))
        ys.append(1 if rm > 0 else 0)
        scores.append(sc if sc is not None else np.nan)
        rmults.append(rm)
        bar_dates.append((r.get("bar_date") or "")[:10])
        end_dates.append((r.get("exit_date") or r.get("bar_date") or "")[:10])   # 標籤實現日（持有到出場）
        # ★ 2026-11-11 M02：exit_date 缺時上行 fallback 成 bar_date 會低估標籤成熟時間。
        # 這裡誠實記錄「實現日是否可靠」——日期感知驗證（_walkforward_eval_dated）據此把
        # 缺實現日的樣本隔離出訓練段（quarantine），不冒充當日已成熟。加法式欄位、不影響既有路徑。
        end_known.append(bool(r.get("exit_date")))
    n = len(parsed)
    if n < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient", "n": n, "direction": direction}
    return {"ok": True, "parsed": parsed, "y": np.array(ys, dtype=float),
            "score": np.array(scores, dtype=float), "rmult": np.array(rmults, dtype=float),
            "bar_dates": bar_dates, "end_dates": end_dates, "end_known": end_known,
            "n": n, "direction": direction}


def _select_keys(parsed):
    """在「給定的樣本集合」（應只傳訓練段）上選覆蓋率夠的特徵鍵，並決定是否納入 score。
    只看特徵是否出現、不看標籤 y，不會造成標籤洩漏。"""
    n = len(parsed)
    if n == 0:
        return [], False
    cnt = {}
    scnt = 0
    for f, s in parsed:
        for k in f:
            cnt[k] = cnt.get(k, 0) + 1
        if s is not None:
            scnt += 1
    keys = sorted([k for k, c in cnt.items() if c / n >= MIN_COVERAGE])
    use_score = (scnt / n) >= MIN_COVERAGE
    return keys, use_score


def _fit_prep(parsed, keys, use_score):
    """在「訓練段」上計算中位數填補值（缺值用）。驗證段只會套用這些值、不重算。"""
    med = {}
    for k in keys:
        vals = [f[k] for f, _ in parsed if f.get(k) is not None]
        med[k] = float(np.median(vals)) if vals else 0.0
    svals = [s for _, s in parsed if s is not None]
    score_med = float(np.median(svals)) if (use_score and svals) else 0.0
    return med, score_med


def _build_X(parsed, keys, use_score, med, score_med):
    """用給定的鍵與（訓練段算出的）填補值，把樣本展成矩陣。回 (X, cols)。"""
    cols = list(keys) + (["score"] if use_score else [])
    X = np.zeros((len(parsed), len(cols)), dtype=float)
    for i, (f, s) in enumerate(parsed):
        for j, k in enumerate(keys):
            v = f.get(k)
            X[i, j] = v if v is not None else med[k]
        if use_score:
            X[i, len(keys)] = s if s is not None else score_med
    return X, cols


def _standardize(X, mean=None, std=None):
    if mean is None:
        mean = X.mean(axis=0)
    if std is None:
        std = X.std(axis=0)
    std = np.where(std < 1e-9, 1.0, std)
    return (X - mean) / std, mean, std


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit_logreg(X, y, l2=1.0, lr=0.2, iters=600):
    """標準化後的 X（不含截距）訓練邏輯回歸，回 (w, b)。簡單批次梯度下降＋L2。"""
    n, d = X.shape
    w = np.zeros(d); b = 0.0
    for _ in range(iters):
        p = _sigmoid(X @ w + b)
        g = p - y
        gw = X.T @ g / n + l2 * w / n
        gb = g.mean()
        w -= lr * gw; b -= lr * gb
    return w, b


def _auc(scores, y):
    """rank-based AUC（Mann–Whitney）；忽略 NaN。無正負樣本回 None。"""
    s = np.asarray(scores, dtype=float); y = np.asarray(y)
    m = np.isfinite(s)
    s, y = s[m], y[m]
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    # 平手取平均秩
    _, inv, cnt = np.unique(s, return_inverse=True, return_counts=True)
    sumr = np.zeros(len(cnt)); np.add.at(sumr, inv, ranks)
    ranks = (sumr / cnt)[inv]
    auc = (ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    return float(auc)


def _walkforward_eval(parsed, y, scores, rmult, k: int = 5, l2: float = 1.0, mode: str = "all",
                      exclude=None) -> dict:
    """★ F-06／Phase 2（2026-11-04）：共用的 expanding-window walk-forward 評估核心。
    把依時間排序的樣本切成 k+1 段，第 j 次用「前 j 段」訓練、對「第 j+1 段」預測（1≤j≤k）；
    每個 fold 的特徵選擇、中位數填補、標準化都只 fit 在該次訓練段，杜絕未來資料洩漏。
    mode 決定特徵組合（供多模型競賽用）：
      - "all"     ：規則分數 score ＋ 全部覆蓋率足夠的特徵
      - "features"：只用特徵、不含規則分數
      - "rule"    ：只用規則分數（＝規則基準當成一個模型）
    回傳 out-of-fold 指標與「用全資料擬合的最終模型」係數（供解釋與落地）。"""
    _excl = set(exclude or [])

    def _mode_keys(keys, use_score):
        keys = [kk for kk in keys if kk not in _excl]    # ★ ablation：去除指定因子（每個 fold 與最終模型一致）
        if mode == "rule":
            return [], True
        if mode == "features":
            return keys, False
        return keys, use_score

    n = len(parsed)
    kk = max(2, k)
    block = n // (kk + 1)
    oof = np.full(n, np.nan)
    if block >= 1:
        for j in range(1, kk + 1):
            tr_end = j * block
            va_end = (j + 1) * block if j < kk else n
            train = parsed[:tr_end]
            keys, use_score = _mode_keys(*_select_keys(train))   # 只在訓練段選特徵
            if not keys and not use_score:
                continue
            med, score_med = _fit_prep(train, keys, use_score)    # 只在訓練段算填補值
            Xtr, _ = _build_X(train, keys, use_score, med, score_med)
            Xva, _ = _build_X(parsed[tr_end:va_end], keys, use_score, med, score_med)
            Xtr_s, mean, std = _standardize(Xtr)                  # 只在訓練段算標準化參數
            w, b = _fit_logreg(Xtr_s, y[:tr_end], l2=l2)
            Xva_s, _, _ = _standardize(Xva, mean, std)            # 驗證段只套用
            oof[tr_end:va_end] = _sigmoid(Xva_s @ w + b)
    val = np.isfinite(oof)
    model_auc = _auc(oof[val], y[val]) if val.any() else None
    base_auc = _auc(scores[val], y[val]) if val.any() else None   # 基準：同一批驗證樣本、只用規則分數
    brier = round(float(np.mean((oof[val] - y[val]) ** 2)), 4) if val.any() else None
    # ★ 2026-11-11（因子 ablation）：OOS「選中淨 R」＝模型在驗證段判為會贏（prob>0.5）的那些訊號，
    # 其實算淨 R 平均。這是審查 §三 3.3 要的「樣本外淨期望值」，供去一因子比較貢獻。
    sel = val & (oof > 0.5)
    oos_sel_n = int(sel.sum())
    oos_sel_net_r = round(float(rmult[sel].mean()), 3) if oos_sel_n >= 1 else None
    deciles = []
    vidx = np.where(val)[0]
    if len(vidx) >= 10:
        order = vidx[np.argsort(oof[vidx], kind="mergesort")]
        m = len(order)
        for g in range(10):
            gi = order[g * m // 10:(g + 1) * m // 10]
            if len(gi):
                deciles.append({"decile": g + 1, "n": int(len(gi)),
                                "win_rate": round(float(y[gi].mean()) * 100, 1),
                                "avg_r": round(float(rmult[gi].mean()), 3)})
    # ── 最終模型（全資料，同 mode）＋係數（標準化空間） ──
    keys_all, use_score_all = _mode_keys(*_select_keys(parsed))
    med_all, score_med_all = _fit_prep(parsed, keys_all, use_score_all)
    Xall, cols = _build_X(parsed, keys_all, use_score_all, med_all, score_med_all)
    if cols:
        Xs, mean, std = _standardize(Xall)
        w, b = _fit_logreg(Xs, y, l2=l2)
    else:
        mean = std = w = np.zeros(0); b = 0.0
    return {"n_eval": int(val.sum()), "model_auc": model_auc, "base_auc": base_auc, "brier": brier,
            "oos_sel_net_r": oos_sel_net_r, "oos_sel_n": oos_sel_n,
            "deciles": deciles, "cols": cols, "mean": mean, "std": std, "w": w, "b": b}


def _walkforward_eval_dated(parsed, y, scores, rmult, bar_dates, end_dates, end_known=None,
                            k: int = 5, l2: float = 1.0, mode: str = "all",
                            exclude=None, embargo_days: int = None) -> dict:
    """★ 2026-11-11 M01/M02 第一刀：日期感知的 purged expanding-window 驗證核心（研究對照用）。

    與既有 _walkforward_eval（按筆數 n//(k+1) 切、無 purge）**並列對照、不取代**——
    把日期感知版設為預設會改研究報表數字，需使用者核准後另行切換（見 DECISION_LOG）。

    規則（對應審查 M01/M02）：
      - 按「決策日」切 k+1 段：同一 bar_date 永不跨 train/val 邊界。
      - Purge：訓練樣本的標籤實現日 end_date 必須 < 該折驗證期第一個決策日 − embargo_days，
        否則自訓練段剔除（訓練標籤在驗證決策日後才成熟＝洩漏）。
      - M02 隔離：end_known=False（exit_date 缺、實現日不可靠）的樣本不進任何訓練段
        （quarantined），不以 bar_date fallback 冒充當日成熟；仍可作驗證樣本（其 y 為實際結果）。
      - protocol：逐折記錄 train/val 日期範圍、原始/有效訓練數、purged、quarantined——
        對應 M01「記錄每折刪除數、日期與有效樣本」與 evaluation_protocol 要求。
    """
    _excl = set(exclude or [])
    if embargo_days is None:
        embargo_days = EMBARGO_DAYS
    n = len(parsed)
    ek = list(end_known) if end_known is not None else [True] * n
    bo = [_date_ord(d) for d in bar_dates]
    eo = [_date_ord(d) for d in end_dates]

    def _mode_keys(sel):
        keys, use_score = _select_keys(sel)
        keys = [kk for kk in keys if kk not in _excl]
        if mode == "rule":
            return [], True
        if mode == "features":
            return keys, False
        return keys, use_score

    # 依決策日切 k+1 段（每段約 n/(k+1) 筆，但整日不拆）
    days = sorted({d for d in bar_dates if d})
    kk_ = max(2, k)
    if len(days) < kk_ + 1:
        return {"ok": False, "reason": "too_few_days", "n_days": len(days)}
    by_day = {}
    for i, d in enumerate(bar_dates):
        by_day.setdefault(d, []).append(i)
    target = n / (kk_ + 1)
    blocks, cur, cnt = [], [], 0
    for d in days:
        cur.append(d); cnt += len(by_day[d])
        if cnt >= target and len(blocks) < kk_:
            blocks.append(cur); cur, cnt = [], 0
    if cur:
        blocks.append(cur)
    if len(blocks) < kk_ + 1:
        return {"ok": False, "reason": "too_few_days", "n_days": len(days)}

    oof = np.full(n, np.nan)
    protocol = {"split": "by_decision_day", "embargo_days": embargo_days, "folds": [],
                "note": "train.label_available_at(end_date) < validation.first_decision_date − embargo；"
                        "end_date 缺（end_known=False）的樣本隔離出訓練段"}
    for j in range(1, len(blocks)):
        train_days = [d for b in blocks[:j] for d in b]
        val_days = blocks[j]
        cut = _date_ord(val_days[0])
        if cut is not None:
            cut -= embargo_days
        tr_all = [i for d in train_days for i in by_day[d]]
        tr_q = [i for i in tr_all if not ek[i] or eo[i] is None]                 # 隔離：實現日不可靠
        tr_p = [i for i in tr_all if i not in set(tr_q) and (cut is None or eo[i] >= cut)]  # purge：標籤太晚成熟
        tr = [i for i in tr_all if i not in set(tr_q) and i not in set(tr_p)]
        va = [i for d in val_days for i in by_day[d]]
        protocol["folds"].append({"train_days": [train_days[0], train_days[-1]],
                                  "val_days": [val_days[0], val_days[-1]],
                                  "n_train_raw": len(tr_all), "n_train": len(tr),
                                  "n_purged": len(tr_p), "n_quarantined": len(tr_q), "n_val": len(va)})
        if not tr or not va:
            continue
        sel = [parsed[i] for i in tr]
        keys, use_score = _mode_keys(sel)
        if not keys and not use_score:
            continue
        med, score_med = _fit_prep(sel, keys, use_score)
        Xtr, _ = _build_X(sel, keys, use_score, med, score_med)
        Xva, _ = _build_X([parsed[i] for i in va], keys, use_score, med, score_med)
        Xtr_s, mean, std = _standardize(Xtr)
        w, b = _fit_logreg(Xtr_s, y[np.array(tr)], l2=l2)
        Xva_s, _, _ = _standardize(Xva, mean, std)
        pv = _sigmoid(Xva_s @ w + b)
        for z, i in enumerate(va):
            oof[i] = pv[z]
    val = np.isfinite(oof)
    model_auc = _auc(oof[val], y[val]) if val.any() else None
    base_auc = _auc(scores[val], y[val]) if val.any() else None
    brier = round(float(np.mean((oof[val] - y[val]) ** 2)), 4) if val.any() else None
    sel_m = val & (oof > 0.5)
    oos_sel_n = int(sel_m.sum())
    oos_sel_net_r = round(float(rmult[sel_m].mean()), 3) if oos_sel_n >= 1 else None
    # ★ 2026-11-12 M01 口徑切換（使用者核准）：dated 成為主口徑，補齊與 legacy 同格式的十分位
    deciles = []
    vidx = np.where(val)[0]
    if len(vidx) >= 10:
        order = vidx[np.argsort(oof[vidx], kind="mergesort")]
        m = len(order)
        for g in range(10):
            gi = order[g * m // 10:(g + 1) * m // 10]
            if len(gi):
                deciles.append({"decile": g + 1, "n": int(len(gi)),
                                "win_rate": round(float(y[gi].mean()) * 100, 1),
                                "avg_r": round(float(rmult[gi].mean()), 3)})
    return {"ok": True, "n_eval": int(val.sum()),
            "model_auc": round(model_auc, 4) if model_auc is not None else None,
            "base_auc": round(base_auc, 4) if base_auc is not None else None,
            "brier": brier, "oos_sel_net_r": oos_sel_net_r, "oos_sel_n": oos_sel_n,
            "deciles": deciles, "protocol": protocol}


def factor_ablation(direction: str = "buy", k: int = 5, l2: float = 1.0) -> dict:
    """因子 ablation（§三 3.3）：逐一拿掉單一因子重訓，看「樣本外淨期望值」有沒有變差。
    delta_net_r = 全特徵 OOS 選中淨 R − 去除該因子後 OOS 選中淨 R：
      >0 ＝ 該因子對樣本外淨期望 R 有正貢獻（值得保留/接入）；
      <0 ＝ 拿掉反而更好（可能只是雜訊或與他因子重複計分）。
    純研究、walk-forward（擴張視窗、per-fold fit、杜絕洩漏）；多空分開、不接正式訊號。"""
    ds = build_dataset(direction)
    if not ds.get("ok"):
        return {"ok": False, "reason": ds.get("reason", "insufficient"),
                "n": ds.get("n", 0), "min_samples": MIN_SAMPLES, "direction": direction}
    parsed, y, score, rmult = ds["parsed"], ds["y"], ds["score"], ds["rmult"]
    keys, _use = _select_keys(parsed)            # 覆蓋率足夠的候選因子（不含 score；score 以 base_auc 另行比較）
    full = _walkforward_eval(parsed, y, score, rmult, k=k, l2=l2, mode="all")
    base_net = full.get("oos_sel_net_r")
    base_auc = full.get("model_auc")
    factors = []
    for kk in keys:
        drop = _walkforward_eval(parsed, y, score, rmult, k=k, l2=l2, mode="all", exclude=[kk])
        d_net = (None if base_net is None or drop.get("oos_sel_net_r") is None
                 else round(base_net - drop["oos_sel_net_r"], 3))
        d_auc = (None if base_auc is None or drop.get("model_auc") is None
                 else round(base_auc - drop["model_auc"], 3))
        factors.append({"factor": kk, "delta_net_r": d_net, "delta_auc": d_auc,
                        "drop_net_r": drop.get("oos_sel_net_r"), "drop_auc": drop.get("model_auc"),
                        "drop_sel_n": drop.get("oos_sel_n")})
    factors.sort(key=lambda z: (z["delta_net_r"] is None, -(z["delta_net_r"] or 0.0)))
    return {"ok": True, "direction": direction, "n": ds["n"], "n_eval": full.get("n_eval"),
            "baseline": {"oos_sel_net_r": base_net, "oos_sel_n": full.get("oos_sel_n"),
                         "model_auc": base_auc, "rule_auc": full.get("base_auc")},
            "factors": factors, "k": k, "l2": l2,
            "metric": "leave-one-factor-out：全特徵 OOS 選中淨 R − 去除該因子後 OOS 選中淨 R（prob>0.5 選中樣本的平均實算淨 R）",
            "disclaimer": "walk-forward（擴張視窗、per-fold fit）去一因子重訓。delta_net_r>0＝該因子對樣本外"
                          "淨期望 R 有正貢獻，<0＝拿掉反而更好（可能雜訊/重複計分）。樣本期間短、單一 fold "
                          "噪音大，delta 僅供觀察、非保證；多空分開、不接正式訊號。"}


def train_and_eval(k: int = 5, l2: float = 1.0, persist: bool = True) -> dict:
    """訓練＋時間序列 walk-forward 交叉驗證＋和規則分數基準比較；可選擇把最終模型存進 meta。
    最終落地模型用全部資料擬合（供日後 shadow 評分）；persist 時另把本次版本追加進版本歷史。"""
    ds = build_dataset()
    if not ds.get("ok"):
        return {"ok": False, "reason": ds.get("reason", "insufficient"), "n": ds.get("n", 0),
                "min_samples": MIN_SAMPLES}
    parsed = ds["parsed"]; y = ds["y"]; scores = ds["score"]; rmult = ds["rmult"]
    n = len(parsed)
    ev = _walkforward_eval(parsed, y, scores, rmult, k=k, l2=l2, mode="all")
    cols, w = ev["cols"], ev["w"]
    coef = sorted([{"feature": cols[i], "weight": round(float(w[i]), 4)} for i in range(len(cols))],
                  key=lambda z: -abs(z["weight"]))
    # ★ 2026-11-12 M01/M02 口徑切換（使用者核准「一和二可以做」）：主口徑＝日期感知 purged
    # walk-forward（_walkforward_eval_dated：按決策日切分、標籤成熟 purge、缺實現日隔離）。
    # 按筆數切分版（審查 M01 指出的洩漏口徑）降為 legacy_count_based 對照、不再是主數字。
    # dated 不可用（資料缺 bar_date／交易日太少）時誠實退回 legacy 並明標，不假裝已切換。
    # 持久化模型不變：最終權重本就是全資料擬合（與切折方式無關），僅評估數字換口徑。
    try:
        evd = _walkforward_eval_dated(parsed, y, scores, rmult,
                                      ds.get("bar_dates") or [], ds.get("end_dates") or [],
                                      ds.get("end_known"), k=k, l2=l2, mode="all")
    except Exception as _e:
        evd = {"ok": False, "error": str(_e)}
    dated_ok = bool(isinstance(evd, dict) and evd.get("ok"))
    pri = evd if dated_ok else ev
    ma, ba = pri["model_auc"], pri["base_auc"]
    out = {"ok": True, "n": n, "direction": ds.get("direction", "buy"),
           "n_eval": pri["n_eval"],
           "eval_method": "dated_purged_walkforward" if dated_ok else "time_series_walkforward",
           "win_base_rate": round(float(y.mean()) * 100, 1),
           "model_auc": round(ma, 4) if ma is not None else None,
           "baseline_auc_rule_score": round(ba, 4) if ba is not None else None,
           "auc_lift": round(ma - ba, 4) if (ma is not None and ba is not None) else None,
           "brier": pri["brier"], "deciles": pri["deciles"], "top_features": coef[:15],
           "n_features": len(cols), "trained_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    out["dated"] = evd
    if dated_ok:
        lma, lba = ev["model_auc"], ev["base_auc"]
        out["legacy_count_based"] = {
            "n_eval": ev["n_eval"],
            "model_auc": round(lma, 4) if lma is not None else None,
            "base_auc": round(lba, 4) if lba is not None else None,
            "brier": ev["brier"],
            "note": "按筆數 n//(k+1) 切分、無 purge（審查 M01 指出的洩漏口徑）——僅供對照，"
                    "與主數字差距大代表原評估受標籤成熟洩漏影響。"}
        out["dated_note"] = ("主口徑＝dated（按決策日切分＋purge：訓練標籤實現日須早於驗證首日"
                             "−embargo＋缺實現日隔離；2026-11-12 使用者核准切換）；"
                             "legacy_count_based 為舊按筆數口徑對照。")
    else:
        out["dated_note"] = ("dated 口徑不可用（" + str(evd.get("reason") or evd.get("error") or "原因不明")
                             + "），本次主數字退回按筆數 walk-forward（誠實標示，非已切換）。")
    if persist:
        try:
            from state_store import store
            store.set_meta(META_KEY, {"keys": cols, "mean": ev["mean"].tolist(), "std": ev["std"].tolist(),
                                      "w": w.tolist(), "b": float(ev["b"]), "trained_at": out["trained_at"],
                                      "n": n, "model_auc": out["model_auc"],
                                      "baseline_auc": out["baseline_auc_rule_score"]})
            out["persisted"] = True
            _save_version_safe(out)      # Phase 2：追加版本歷史（失敗不影響訓練本身）
        except Exception as e:
            out["persisted"] = False; out["persist_error"] = str(e)
    return out


def load_champion() -> Optional[dict]:
    """載入目前 champion 模型（META_KEY）。供 shadow 平行評分一次載入、批次使用。無模型回 None。"""
    try:
        from state_store import store
        m = store.get_meta(META_KEY)
        if not m or not m.get("keys"):
            return None
        return m
    except Exception as e:
        logger.warning(f"load_champion: {e}")
        return None


def prob_with_model(m: Optional[dict], feat: dict, score: Optional[float] = None) -> Optional[float]:
    """用「已載入」的模型 dict 對一筆特徵算勝率（0~1）。無模型回 None。"""
    try:
        if not m or not m.get("keys"):
            return None
        keys, mean, std = m["keys"], np.array(m["mean"]), np.array(m["std"])
        w, b = np.array(m["w"]), float(m["b"])
        std = np.where(std < 1e-9, 1.0, std)
        x = np.zeros(len(keys))
        for j, k in enumerate(keys):
            v = _num(score) if k == "score" else _num((feat or {}).get(k))
            x[j] = v if v is not None else mean[j]      # 缺值用訓練均值（標準化後≈0）
        z = (x - mean) / std
        return float(_sigmoid(float(z @ w) + b))
    except Exception as e:
        logger.warning(f"prob_with_model: {e}")
        return None


def score_features(feat: dict, score: Optional[float] = None) -> Optional[float]:
    """用已存的 champion 模型對一筆特徵算勝率（0~1）。無模型回 None。
    注意：此函式不影響即時訊號的產生；僅供 shadow 平行記錄 model_prob 與解釋用。"""
    return prob_with_model(load_champion(), feat, score)


# ═══════════════════════ Phase 2：量化研究升級（2026-11-04） ═══════════════════════
# 全部屬「離線評估／比較／記錄」，不改動即時訊號、不自動把任何模型晉升為正式champion
# （維持使用者「用 c」暫停自動進化的決定）。晉升與否仍由使用者手動決定。

# 多模型競賽的參賽設定：同一批資料、同一套時序 walk-forward，只改特徵組合與正則強度。
_COMPETITION_CONFIGS = [
    {"id": "rule_only",      "label": "純規則分數（基準）",      "mode": "rule",     "l2": 1.0},
    {"id": "features_only",  "label": "只用特徵（不含規則分數）", "mode": "features", "l2": 1.0},
    {"id": "rule_feats_l2_1", "label": "規則＋特徵（標準正則）",  "mode": "all",      "l2": 1.0},
    {"id": "rule_feats_l2_03", "label": "規則＋特徵（較弱正則）", "mode": "all",      "l2": 0.3},
    {"id": "rule_feats_l2_3", "label": "規則＋特徵（較強正則）",  "mode": "all",      "l2": 3.0},
]


def run_competition(k: int = 5) -> dict:
    """多模型競賽：用同一批已結算 shadow 資料與同一套時序 walk-forward，評估多種特徵組合／
    正則強度，產出排行榜（依 out-of-fold AUC 由高到低，平手比 Brier 低者）。只評估比較、
    不改 champion、不自動晉升。"""
    ds = build_dataset()
    if not ds.get("ok"):
        return {"ok": False, "reason": ds.get("reason", "insufficient"), "n": ds.get("n", 0),
                "min_samples": MIN_SAMPLES}
    parsed, y, scores, rmult = ds["parsed"], ds["y"], ds["score"], ds["rmult"]
    entries, base_auc, n_eval = [], None, 0
    for cfg in _COMPETITION_CONFIGS:
        ev = _walkforward_eval(parsed, y, scores, rmult, k=k, l2=cfg["l2"], mode=cfg["mode"])
        if base_auc is None:
            base_auc = ev["base_auc"]       # 同一批驗證樣本的規則基準，各參賽者一致
            n_eval = ev["n_eval"]
        ma = ev["model_auc"]
        entries.append({"id": cfg["id"], "label": cfg["label"], "mode": cfg["mode"], "l2": cfg["l2"],
                        "model_auc": round(ma, 4) if ma is not None else None,
                        "brier": ev["brier"], "n_features": len(ev["cols"]),
                        "auc_lift": round(ma - base_auc, 4) if (ma is not None and base_auc is not None) else None})
    entries.sort(key=lambda e: (-(e["model_auc"] if e["model_auc"] is not None else -1),
                                e["brier"] if e["brier"] is not None else 1.0))
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    return {"ok": True, "n": ds["n"], "n_eval": n_eval,
            "win_base_rate": round(float(y.mean()) * 100, 1),
            "baseline_auc_rule_score": round(base_auc, 4) if base_auc is not None else None,
            "entries": entries, "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


# 模型版本管理：每次正式訓練（persist）就把該版本的評估摘要追加進歷史，供排行榜與回溯。
HISTORY_KEY = "learning_models_history"
MAX_HISTORY = 50


def _save_version_safe(result: dict):
    """把一次訓練結果的摘要追加進版本歷史；任何失敗都只記 log，不影響訓練本身。"""
    try:
        from state_store import store
        st, hist = store.read_meta(HISTORY_KEY)
        hist = hist if (st == "ok" and isinstance(hist, list)) else []
        rec = {"version": len(hist) + 1, "trained_at": result.get("trained_at"),
               "n": result.get("n"), "n_eval": result.get("n_eval"),
               "model_auc": result.get("model_auc"),
               "baseline_auc": result.get("baseline_auc_rule_score"),
               "auc_lift": result.get("auc_lift"), "brier": result.get("brier"),
               "n_features": result.get("n_features"), "eval_method": result.get("eval_method"),
               "top_features": (result.get("top_features") or [])[:5]}
        hist.append(rec)
        store.set_meta(HISTORY_KEY, hist[-MAX_HISTORY:])
    except Exception as e:
        logger.warning(f"_save_version_safe: {e}")


def list_model_versions() -> dict:
    """回傳已訓練模型的版本歷史與排行榜（依 out-of-fold AUC 由高到低）。"""
    try:
        from state_store import store
        st, hist = store.read_meta(HISTORY_KEY)
        if st == "error":
            return {"ok": False, "reason": "risk_state_unavailable", "versions": [], "leaderboard": []}
        hist = hist if (st == "ok" and isinstance(hist, list)) else []
    except Exception as e:
        logger.warning(f"list_model_versions: {e}")
        hist = []
    leaderboard = sorted([h for h in hist if h.get("model_auc") is not None],
                         key=lambda h: -h["model_auc"])
    return {"ok": True, "count": len(hist), "versions": hist, "leaderboard": leaderboard}


def explain_signal(feat: dict, score: Optional[float] = None) -> Optional[dict]:
    """AI 決策解釋：用現行 champion 模型解釋單筆訊號——回傳預測勝率、截距、以及各特徵在
    標準化空間的貢獻（weight × z），依絕對值排序。正貢獻＝推升勝率、負貢獻＝拉低。無模型回 None。"""
    try:
        from state_store import store
        m = store.get_meta(META_KEY)
        if not m or not m.get("keys"):
            return None
        keys, mean, std = m["keys"], np.array(m["mean"]), np.array(m["std"])
        w, b = np.array(m["w"]), float(m["b"])
        std = np.where(std < 1e-9, 1.0, std)
        x = np.zeros(len(keys))
        for j, k in enumerate(keys):
            v = _num(score) if k == "score" else _num((feat or {}).get(k))
            x[j] = v if v is not None else mean[j]
        z = (x - mean) / std
        contrib = w * z
        prob = float(_sigmoid(float(z @ w) + b))
        items = sorted([{"feature": keys[i], "value": round(float(x[i]), 4),
                         "contribution": round(float(contrib[i]), 4)} for i in range(len(keys))],
                       key=lambda d: -abs(d["contribution"]))
        return {"prob": round(prob, 4), "bias": round(b, 4), "n_features": len(keys),
                "trained_at": m.get("trained_at"), "contributions": items}
    except Exception as e:
        logger.warning(f"explain_signal: {e}")
        return None


def explain_latest() -> dict:
    """解釋最近一筆 shadow 訊號（champion 模型）。供儀表板一眼看「模型為何這樣評分」。"""
    try:
        from state_store import store
        import shadow
        shadow.ensure_table()
        with store._conn() as conn:
            row = conn.execute(
                "SELECT code, name, direction, score, features, bar_date FROM shadow_signals "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            return {"ok": False, "reason": "no_signal"}
        row = dict(row)
        try:
            feats = json.loads(row.get("features") or "{}")
        except Exception:
            feats = {}
        ex = explain_signal(feats if isinstance(feats, dict) else {}, score=row.get("score"))
        if ex is None:
            return {"ok": False, "reason": "no_model"}
        ex.update({"ok": True, "code": row.get("code"), "name": row.get("name"),
                   "direction": row.get("direction"), "bar_date": row.get("bar_date"),
                   "rule_score": row.get("score")})
        return ex
    except Exception as e:
        logger.warning(f"explain_latest: {e}")
        return {"ok": False, "reason": "error"}


# ═══════════════ B：半自動挑戰者框架（2026-11-04，使用者選定方案 B） ═══════════════
# 設計原則：
# - 更嚴謹的驗證：Purged（清掉與驗證期重疊持有的樣本）＋ Embargo（隔離帶）walk-forward，
#   外加「獨立 holdout」（最近一段完全不參與訓練/挑選）做最終檢驗——因為每筆標籤要持有數日，
#   不做 purge 會讓重疊持有期洩漏，plain walk-forward 仍可能偏樂觀。
# - 資料關卡：holdout 樣本數／獨立交易日數不足時直接標「資料不足、不建議晉升」，框架拒絕推薦。
# - 手動晉升：挑戰者只有通過關卡、在 holdout 勝過現任 champion 與規則基準，且站主按下採用才上位。
# - champion 僅供 shadow 平行記錄（model_prob）與解釋，絕不影響即時訊號的產生。
CHAMPION_KEY = "learning_champion"
HOLDOUT_FRAC = 0.2
EMBARGO_DAYS = 3
MIN_HOLDOUT = 100
MIN_HOLDOUT_DAYS = 8
PROMOTE_MARGIN = 0.01      # holdout AUC 至少要贏現任/基準這麼多才建議晉升


def _apply_mode(keys, use_score, mode):
    if mode == "rule":
        return [], True
    if mode == "features":
        return keys, False
    return keys, use_score


def _date_ord(s):
    try:
        import datetime as _dt
        return _dt.date.fromisoformat((s or "")[:10]).toordinal()
    except Exception:
        return None


def _eval_config(ds, dev_n, k, l2, mode, embargo):
    """對單一設定做 Purged+Embargo walk-forward（dev 段）＋ 獨立 holdout 檢驗。
    回傳 dev_auc（開發段 out-of-fold）與 holdout_auc/holdout_brier（最終獨立檢驗）。"""
    parsed, y, scores = ds["parsed"], ds["y"], ds["score"]
    bar_ords = [_date_ord(d) for d in ds["bar_dates"]]
    end_ords = [_date_ord(d) for d in ds["end_dates"]]
    n = len(parsed)

    def _fit_eval(tr_idx, te_idx):
        if not tr_idx or not te_idx:
            return None, []
        train = [parsed[i] for i in tr_idx]
        keys, use_score = _apply_mode(*_select_keys(train), mode)
        if not keys and not use_score:
            return None, []
        med, score_med = _fit_prep(train, keys, use_score)
        Xtr, cols = _build_X(train, keys, use_score, med, score_med)
        Xte, _ = _build_X([parsed[i] for i in te_idx], keys, use_score, med, score_med)
        Xtr_s, mean, std = _standardize(Xtr)
        w, b = _fit_logreg(Xtr_s, np.array([y[i] for i in tr_idx]), l2=l2)
        Xte_s, _, _ = _standardize(Xte, mean, std)
        return _sigmoid(Xte_s @ w + b), cols

    # ── dev 段：Purged + Embargo expanding walk-forward ──
    dev_oof = np.full(dev_n, np.nan)
    kk = max(2, k); block = dev_n // (kk + 1)
    if block >= 1:
        for j in range(1, kk + 1):
            tr_end = j * block
            va_end = (j + 1) * block if j < kk else dev_n
            val_ords = [bar_ords[i] for i in range(tr_end, va_end) if bar_ords[i] is not None]
            cut = (min(val_ords) - embargo) if val_ords else None     # 隔離帶：出場日 < 驗證起日−embargo 才可當訓練
            tr_idx = [i for i in range(0, tr_end)
                      if cut is None or end_ords[i] is None or end_ords[i] < cut]
            p, _ = _fit_eval(tr_idx, list(range(tr_end, va_end)))
            if p is not None:
                dev_oof[tr_end:va_end] = p
    dv = np.isfinite(dev_oof)
    dev_auc = _auc(dev_oof[dv], y[:dev_n][dv]) if dv.any() else None

    # ── 獨立 holdout：用 dev 段（對 holdout 做 purge）訓練最終模型、評在 holdout ──
    hold_idx = list(range(dev_n, n))
    hold_auc = hold_brier = None; n_features = 0
    if hold_idx:
        hold_ords = [bar_ords[i] for i in hold_idx if bar_ords[i] is not None]
        hcut = (min(hold_ords) - embargo) if hold_ords else None
        dev_idx = [i for i in range(0, dev_n)
                   if hcut is None or end_ords[i] is None or end_ords[i] < hcut]
        ph, cols = _fit_eval(dev_idx, hold_idx)
        if ph is not None:
            yh = y[hold_idx]
            hold_auc = _auc(ph, yh)
            hold_brier = round(float(np.mean((ph - yh) ** 2)), 4)
            n_features = len(cols)
    return {"dev_auc": dev_auc, "holdout_auc": hold_auc, "holdout_brier": hold_brier, "n_features": n_features}


_M03_ATTEMPTS_KEY = "learning_m03_attempts"


def _challengers_m03(ds, k, embargo_days, champ):
    """★ 2026-11-11 M03 第一刀（並列對照、不改現行行為）：內層 dev 折選模、外層封存只評一次。

    審查 M03 指出：run_challengers 對**所有**設定算 holdout_auc 後排序＝holdout 同時被用來
    「選模型」與「評估」，且與 get_champion 存的舊 holdout_auc 比較＝不同資料期間不可比。

    本協定（與現行 entries/recommendation 並列、預設不切換——切換需使用者核准）：
      - 封存邊界按**日**對齊（最近 ~20% 交易日為封存段，整日不拆；無 bar_date 樣本整組排除）。
      - 內層：各設定只看 dev 段的日期感知 purged OOF 指標（_walkforward_eval_dated）排名選模，
        選模過程完全不接觸封存段。
      - 外層：封存段只評**被選中者＋規則基準＋現任 champion 設定**（同一批封存樣本重評，
        不拿舊期間的 champ holdout_auc 來比），各一次。
      - 晉升建議附校準（brier）、淨 R、樣本量；仍為人工核准、不自動晉升。
      - 誠實標示：這段 holdout 已被歷次執行反覆查看，依審查應視為「開發測試資料」；
        真正封存區需上線後新累積、未被任何挑選查看過的樣本（缺口）。
    """
    parsed, y, scores, rmult = ds["parsed"], ds["y"], ds["score"], ds["rmult"]
    bd = ds.get("bar_dates") or []
    ed = ds.get("end_dates") or []
    n = len(parsed)
    ek = ds.get("end_known") or [True] * n
    days = sorted({d for d in bd if d})
    kk_ = max(2, k)
    if len(days) < kk_ + 2:        # dev 要能切 k+1 塊、封存段至少 1 日
        return {"ok": False, "reason": "too_few_days", "n_days": len(days)}
    n_dev_days = min(len(days) - 1, max(kk_ + 1, int(round(len(days) * (1 - HOLDOUT_FRAC)))))
    dev_days = set(days[:n_dev_days])
    hold_first = days[n_dev_days]
    dev_idx = [i for i in range(n) if bd[i] in dev_days]
    hold_idx = [i for i in range(n) if bd[i] and bd[i] >= hold_first]
    excluded_undated = n - len(dev_idx) - len(hold_idx)
    hold_days_cnt = len(days) - n_dev_days
    if not dev_idx or not hold_idx:
        return {"ok": False, "reason": "empty_split"}
    _i = np.array(dev_idx)
    p_dev = [parsed[i] for i in dev_idx]
    bd_dev = [bd[i] for i in dev_idx]
    ed_dev = [ed[i] for i in dev_idx]
    ek_dev = [ek[i] for i in dev_idx]
    y_dev, s_dev, r_dev = y[_i], scores[_i], rmult[_i]

    # ── 內層：dev 段日期感知 OOF 排名（選模不碰封存段）──
    inner = []
    for cfg in _COMPETITION_CONFIGS:
        try:
            ev = _walkforward_eval_dated(p_dev, y_dev, s_dev, r_dev, bd_dev, ed_dev, ek_dev,
                                         k=k, l2=cfg["l2"], mode=cfg["mode"],
                                         embargo_days=embargo_days)
        except Exception as e:
            ev = {"ok": False, "error": str(e)}
        inner.append({"id": cfg["id"], "label": cfg["label"], "mode": cfg["mode"], "l2": cfg["l2"],
                      "dev_auc": ev.get("model_auc"), "dev_brier": ev.get("brier"),
                      "dev_n_eval": ev.get("n_eval"), "dev_ok": bool(ev.get("ok"))})
    inner.sort(key=lambda e: (-(e["dev_auc"] if e["dev_auc"] is not None else -1.0),
                              (e["dev_brier"] if e["dev_brier"] is not None else 9.0)))
    for i, e in enumerate(inner):
        e["dev_rank"] = i + 1
    selected = next((e for e in inner if e["dev_auc"] is not None), None)

    # ── 外層：封存段只評一次（選中者＋規則基準＋現任 champion 同批重評）──
    eo_all = [_date_ord(d) for d in ed]
    hcut = _date_ord(hold_first)
    if hcut is not None:
        hcut -= embargo_days

    def _sealed_eval(l2, mode):
        n_q = sum(1 for i in dev_idx if (not ek[i]) or eo_all[i] is None)      # M02 隔離
        tr = [i for i in dev_idx if ek[i] and eo_all[i] is not None
              and (hcut is None or eo_all[i] < hcut)]                          # purge 跨界標籤
        n_p = len(dev_idx) - n_q - len(tr)
        if not tr:
            return None
        sel = [parsed[i] for i in tr]
        keys, use_score = _apply_mode(*_select_keys(sel), mode)
        if not keys and not use_score:
            return None
        med, score_med = _fit_prep(sel, keys, use_score)
        Xtr, _ = _build_X(sel, keys, use_score, med, score_med)
        Xho, _ = _build_X([parsed[i] for i in hold_idx], keys, use_score, med, score_med)
        Xtr_s, mean, std = _standardize(Xtr)
        w, b = _fit_logreg(Xtr_s, y[np.array(tr)], l2=l2)
        Xho_s, _, _ = _standardize(Xho, mean, std)
        ph = _sigmoid(Xho_s @ w + b)
        yh = y[np.array(hold_idx)]
        rh = rmult[np.array(hold_idx)]
        auc = _auc(ph, yh)
        selm = ph > 0.5
        sn = int(selm.sum())
        return {"auc": round(auc, 4) if auc is not None else None,
                "brier": round(float(np.mean((ph - yh) ** 2)), 4),
                "sel_net_r": round(float(rh[selm].mean()), 3) if sn >= 1 else None, "sel_n": sn,
                "n_train": len(tr), "n_purged": n_p, "n_quarantined": n_q}

    sealed = {"selected": None, "baseline_rule": None, "champion_same_batch": None}
    try:
        if selected is not None:
            sealed["selected"] = _sealed_eval(selected["l2"], selected["mode"])
        sealed["baseline_rule"] = _sealed_eval(1.0, "rule")
        champ_cfg = next((c for c in _COMPETITION_CONFIGS
                          if c["id"] == (champ or {}).get("config_id")), None)
        if champ_cfg is not None:
            sealed["champion_same_batch"] = _sealed_eval(champ_cfg["l2"], champ_cfg["mode"])
    except Exception as e:
        logger.warning(f"_challengers_m03 sealed: {e}")

    gates_ok = (len(hold_idx) >= MIN_HOLDOUT and hold_days_cnt >= MIN_HOLDOUT_DAYS)
    sa = (sealed["selected"] or {}).get("auc")
    ba = (sealed["baseline_rule"] or {}).get("auc")
    ca = (sealed["champion_same_batch"] or {}).get("auc")
    if not gates_ok:
        rec = {"promote": None, "reason": f"資料不足（封存段 {len(hold_idx)} 筆／{hold_days_cnt} 個交易日，"
               f"需 ≥{MIN_HOLDOUT} 筆且 ≥{MIN_HOLDOUT_DAYS} 個交易日），不建議晉升"}
    elif selected is None or sa is None:
        rec = {"promote": None, "reason": "內層無法選模或封存段無法評估"}
    else:
        beat_rule = (ba is None) or (sa - ba >= PROMOTE_MARGIN)
        beat_champ = (ca is None) or (sa - ca >= PROMOTE_MARGIN)
        if beat_rule and beat_champ:
            rec = {"promote": selected["id"],
                   "reason": "內層 dev 折選出、封存段一次性評估勝過規則基準與現任 champion（同批重評）；"
                             "仍需站主手動核准採用"}
        else:
            rec = {"promote": None, "reason": "封存段一次性評估未明顯優於規則基準或現任 champion（同批重評），維持不變"}
    return {"ok": True, "inner_dev_rank": inner, "selected_id": (selected or {}).get("id"),
            "sealed_once": sealed, "recommendation": rec, "gates_ok": gates_ok,
            "protocol": {"split": "day_aligned_holdout", "selection": "inner_dev_oof_only",
                         "dev_days": n_dev_days, "holdout_days": hold_days_cnt,
                         "holdout_first": hold_first, "n_dev": len(dev_idx),
                         "n_holdout": len(hold_idx), "excluded_undated": excluded_undated,
                         "embargo_days": embargo_days},
            "holdout_note": "此封存段（最近約 20% 交易日）已被歷次挑戰者執行反覆查看，依審查 M03 應視為"
                            "「開發測試資料」；真正封存區需上線後新累積、未被任何挑選查看過的樣本（缺口，"
                            "另行估工）。attempts 為歷來查看次數。"}


def get_champion() -> Optional[dict]:
    """目前已採用的 champion 中繼資料（不含權重；權重在 META_KEY）。無則回 None。"""
    try:
        from state_store import store
        st, c = store.read_meta(CHAMPION_KEY)
        return c if (st == "ok" and isinstance(c, dict)) else None
    except Exception as e:
        logger.warning(f"get_champion: {e}")
        return None


def run_challengers(k: int = 5, embargo_days: int = EMBARGO_DAYS) -> dict:
    """半自動挑戰者評估。主口徑（2026-11-12 使用者核准切換）＝M03 協定：內層 dev 折選模（日期感知
    purged OOF、不碰封存段）、封存段只評一次（選中者＋規則基準＋現任 champion 同批重評）。
    舊 holdout 排名口徑（選模與評估同批＝審查 M03 指出的缺陷）降為 legacy_holdout_ranked 對照；
    m03 不可用時誠實退回舊口徑並明標。仍為人工核准、不自動晉升。"""
    ds = build_dataset()
    if not ds.get("ok"):
        return {"ok": False, "reason": ds.get("reason", "insufficient"), "n": ds.get("n", 0),
                "min_samples": MIN_SAMPLES}
    n = ds["n"]
    dev_n = max(1, int(round(n * (1 - HOLDOUT_FRAC))))
    n_holdout = n - dev_n
    hold_days = len(set(d for d in ds["bar_dates"][dev_n:] if d))
    base = _eval_config(ds, dev_n, k, 1.0, "rule", embargo_days)
    base_auc = base["holdout_auc"]
    entries = []
    for cfg in _COMPETITION_CONFIGS:
        ev = _eval_config(ds, dev_n, k, cfg["l2"], cfg["mode"], embargo_days)
        entries.append({"id": cfg["id"], "label": cfg["label"], "mode": cfg["mode"], "l2": cfg["l2"],
                        "dev_auc": round(ev["dev_auc"], 4) if ev["dev_auc"] is not None else None,
                        "holdout_auc": round(ev["holdout_auc"], 4) if ev["holdout_auc"] is not None else None,
                        "holdout_brier": ev["holdout_brier"], "n_features": ev["n_features"]})
    entries.sort(key=lambda e: -(e["holdout_auc"] if e["holdout_auc"] is not None else -1))
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    gates_ok = (n_holdout >= MIN_HOLDOUT and hold_days >= MIN_HOLDOUT_DAYS)
    champ = get_champion()
    champ_auc = (champ or {}).get("holdout_auc")
    top = entries[0] if entries else None
    if not gates_ok:
        rec = {"promote": None, "reason": f"資料不足（holdout {n_holdout} 筆／{hold_days} 個交易日，"
               f"需 ≥{MIN_HOLDOUT} 筆且 ≥{MIN_HOLDOUT_DAYS} 個交易日），為避免過擬合不建議晉升"}
    elif top and top["holdout_auc"] is not None:
        beat_champ = (champ_auc is None) or (top["holdout_auc"] - champ_auc >= PROMOTE_MARGIN)
        beat_rule = (base_auc is None) or (top["holdout_auc"] - base_auc >= PROMOTE_MARGIN)
        if beat_champ and beat_rule:
            rec = {"promote": top["id"], "reason": "通過資料關卡，且在獨立 holdout 上勝過現任 champion 與規則基準；可由站主手動採用"}
        else:
            rec = {"promote": None, "reason": "未明顯優於現任 champion 或規則基準，維持不變"}
    else:
        rec = {"promote": None, "reason": "holdout 無法評估"}
    # ★ 2026-11-12 M03 協定切換（使用者核准「一和二可以做」）：m03 成為主口徑——
    #   內層 dev 折選模（不碰封存段）、封存段只評一次（選中者＋規則基準＋現任 champion 同批重評）。
    #   舊「所有設定算 holdout_auc 後排序」口徑（審查 M03 指出的缺陷：選模與評估同批）降為
    #   legacy_holdout_ranked 對照；m03 不可用（交易日太少等）時誠實退回 legacy 並明標。
    try:
        m03 = _challengers_m03(ds, k, embargo_days, champ)
    except Exception as e:
        logger.warning(f"run_challengers m03: {e}")
        m03 = {"ok": False, "error": str(e)}
    try:
        from state_store import store
        st, att = store.read_meta(_M03_ATTEMPTS_KEY)
        cnt_att = int((att or {}).get("count", 0)) + 1 if st == "ok" else 1
        store.set_meta(_M03_ATTEMPTS_KEY, {"count": cnt_att,
                                           "last_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        m03["attempts"] = cnt_att
    except Exception as e:
        logger.warning(f"run_challengers m03 attempts: {e}")
    legacy = {"dev_n": dev_n, "n_holdout": n_holdout, "holdout_days": hold_days,
              "gates_ok": gates_ok,
              "baseline_holdout_auc": round(base_auc, 4) if base_auc is not None else None,
              "entries": entries, "recommendation": rec,
              "note": "舊口徑：所有設定算 holdout_auc 後排序＝選模與評估同一批封存樣本"
                      "（審查 M03 指出的缺陷）；2026-11-12 起僅供對照、不再是主口徑。"}
    out = {"ok": True, "n": n, "direction": ds.get("direction", "buy"),
           "embargo_days": embargo_days, "champion": champ, "m03": m03,
           "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if m03.get("ok"):
        p = m03.get("protocol") or {}
        out.update({
            "protocol_default": "m03_inner_dev_sealed_once",
            "gates_ok": m03.get("gates_ok"),
            "recommendation": m03.get("recommendation"),
            "entries": [{**e, "rank": e.get("dev_rank")} for e in (m03.get("inner_dev_rank") or [])],
            "dev_n": p.get("n_dev"), "n_holdout": p.get("n_holdout"),
            "holdout_days": p.get("holdout_days"),
            "baseline_holdout_auc": ((m03.get("sealed_once") or {}).get("baseline_rule") or {}).get("auc"),
            "legacy_holdout_ranked": legacy,
            "m03_note": "主口徑＝M03 協定（內層 dev 折選模、封存段只評一次、champion 同批重評；"
                        "2026-11-12 使用者核准切換）；legacy_holdout_ranked 為舊缺陷口徑對照。"})
    else:
        out.update({
            "protocol_default": "legacy_holdout_ranked",
            "gates_ok": gates_ok, "recommendation": rec, "entries": entries,
            "dev_n": dev_n, "n_holdout": n_holdout, "holdout_days": hold_days,
            "baseline_holdout_auc": round(base_auc, 4) if base_auc is not None else None,
            "m03_note": "M03 協定不可用（" + str(m03.get("reason") or m03.get("error") or "原因不明")
                        + "），本次誠實退回舊 holdout 排名口徑（非已切換主口徑的數字）。"})
    return out


def promote_champion(config_id: str) -> dict:
    """站主手動採用某挑戰者為新 champion：先驗證資料關卡（不過就拒絕），通過才用全資料重訓該設定、
    寫入 META_KEY（供 shadow 平行評分）與 CHAMPION_KEY（中繼資料）。不自動、不影響即時訊號。"""
    cfg = next((c for c in _COMPETITION_CONFIGS if c["id"] == config_id), None)
    if not cfg:
        return {"ok": False, "reason": "unknown_config"}
    res = run_challengers()
    if not res.get("ok"):
        return {"ok": False, "reason": res.get("reason", "insufficient")}
    if not res["gates_ok"]:
        return {"ok": False, "reason": "資料未通過關卡，為避免過擬合拒絕晉升", "detail": res["recommendation"]["reason"]}
    ds = build_dataset()
    if not ds.get("ok"):
        return {"ok": False, "reason": "insufficient"}
    ev = _walkforward_eval(ds["parsed"], ds["y"], ds["score"], ds["rmult"], k=5, l2=cfg["l2"], mode=cfg["mode"])
    entry = next((e for e in res["entries"] if e["id"] == config_id), {})
    # ★ 2026-11-12 M03 切換：champion 中繼資料改用 m03 口徑的誠實數字——封存段一次性評估只對
    # 「被選中者」做過，晉升者若非選中者則無封存數字（存 None，不冒用 dev 數字假裝）。
    m03r = res.get("m03") or {}
    sealed_sel = ((m03r.get("sealed_once") or {}).get("selected")
                  if config_id == m03r.get("selected_id") else None)
    holdout_auc = (sealed_sel or {}).get("auc") if m03r.get("ok") else entry.get("holdout_auc")
    try:
        from state_store import store
        trained_at = time.strftime("%Y-%m-%d %H:%M:%S")
        store.set_meta(META_KEY, {"keys": ev["cols"], "mean": ev["mean"].tolist(), "std": ev["std"].tolist(),
                                  "w": ev["w"].tolist(), "b": float(ev["b"]), "trained_at": trained_at,
                                  "n": ds["n"], "model_auc": holdout_auc,
                                  "baseline_auc": res.get("baseline_holdout_auc")})
        champ = {"config_id": config_id, "label": cfg["label"], "mode": cfg["mode"], "l2": cfg["l2"],
                 "holdout_auc": holdout_auc, "baseline_holdout_auc": res.get("baseline_holdout_auc"),
                 "dev_auc": entry.get("dev_auc"), "sealed_brier": (sealed_sel or {}).get("brier"),
                 "sealed_sel_net_r": (sealed_sel or {}).get("sel_net_r"),
                 "metric_note": ("m03 封存段一次性評估（2026-11-12 切換）" if m03r.get("ok")
                                 else "legacy holdout 排名口徑（m03 不可用時退回）"),
                 "n": ds["n"], "n_holdout": res["n_holdout"], "promoted_at": trained_at}
        store.set_meta(CHAMPION_KEY, champ)
        return {"ok": True, "champion": champ}
    except Exception as e:
        logger.error(f"promote_champion: {e}")
        return {"ok": False, "reason": "寫入失敗"}
