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
_EXCLUDE_KEYS = {"reject", "sector", "code", "ticker", "name", "direction", "kind"}


def _rows():
    """讀已結算、有淨 R 的 shadow 列：score / r_multiple / kind / features。
    ★ F-06：依 bar_date（訊號決策日）＋ created_at 由舊到新排序，提供時間序列評估所需的
    時間順序；排序在 SQL 完成，build_dataset 僅保留此順序。"""
    from state_store import store
    import shadow
    shadow.ensure_table()
    with store._conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT bar_date, score, r_multiple, kind, features FROM shadow_signals "
            "WHERE status='closed' AND r_multiple IS NOT NULL "
            "ORDER BY bar_date ASC, created_at ASC").fetchall()]


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except Exception:
        return None


def build_dataset():
    """回傳 {ok, parsed, y, score, rmult, n} 或 {ok:False,...}（資料不足）。
    parsed：依時間由舊到新的 [(特徵字典, 規則分數), ...]（未填補、未標準化——前處理延後到
    各 fold 的訓練段才 fit，避免洩漏）；y：勝(1)/負(0)；score：規則分數；rmult：淨 R。"""
    rows = _rows()
    if len(rows) < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient", "n": len(rows)}
    parsed, ys, scores, rmults = [], [], [], []
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
    n = len(parsed)
    if n < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient", "n": n}
    return {"ok": True, "parsed": parsed, "y": np.array(ys, dtype=float),
            "score": np.array(scores, dtype=float), "rmult": np.array(rmults, dtype=float), "n": n}


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


def train_and_eval(k: int = 5, l2: float = 1.0, persist: bool = True) -> dict:
    """訓練＋時間序列 walk-forward 交叉驗證＋和規則分數基準比較；可選擇把最終模型存進 meta。

    ★ F-06（2026-11-04）：評估改為 expanding-window walk-forward。把依時間排序的樣本切成
    k+1 段，第 j 次用「前 j 段」訓練、對「第 j+1 段」預測（1≤j≤k）；每個 fold 的特徵選擇、
    中位數填補、標準化都只 fit 在該次的訓練段。如此驗證樣本的任何資訊都不會回流到訓練前處理，
    報出的 AUC 才是對「未來、未見過」資料的誠實估計。最終落地模型仍用全部資料擬合（供日後評分）。"""
    ds = build_dataset()
    if not ds.get("ok"):
        return {"ok": False, "reason": ds.get("reason", "insufficient"), "n": ds.get("n", 0),
                "min_samples": MIN_SAMPLES}
    parsed = ds["parsed"]; y = ds["y"]; scores = ds["score"]; rmult = ds["rmult"]
    n = len(parsed)
    kk = max(2, k)
    block = n // (kk + 1)
    oof = np.full(n, np.nan)
    if block >= 1:
        for j in range(1, kk + 1):
            tr_end = j * block
            va_end = (j + 1) * block if j < kk else n
            train = parsed[:tr_end]
            keys, use_score = _select_keys(train)           # 只在訓練段選特徵
            if not keys and not use_score:
                continue
            med, score_med = _fit_prep(train, keys, use_score)   # 只在訓練段算填補值
            Xtr, _ = _build_X(train, keys, use_score, med, score_med)
            Xva, _ = _build_X(parsed[tr_end:va_end], keys, use_score, med, score_med)
            Xtr_s, mean, std = _standardize(Xtr)                 # 只在訓練段算標準化參數
            w, b = _fit_logreg(Xtr_s, y[:tr_end], l2=l2)
            Xva_s, _, _ = _standardize(Xva, mean, std)           # 驗證段只套用
            oof[tr_end:va_end] = _sigmoid(Xva_s @ w + b)
    val = np.isfinite(oof)
    model_auc = _auc(oof[val], y[val]) if val.any() else None
    base_auc = _auc(scores[val], y[val]) if val.any() else None   # 基準：同一批驗證樣本、只用規則分數
    brier = round(float(np.mean((oof[val] - y[val]) ** 2)), 4) if val.any() else None
    # 十分位增益：依模型（out-of-fold）分數分組看勝率與平均 R（只用驗證段）
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
    # ── 最終模型（全資料）＋係數（標準化空間）供解釋與日後 shadow 評分 ──
    keys_all, use_score_all = _select_keys(parsed)
    med_all, score_med_all = _fit_prep(parsed, keys_all, use_score_all)
    Xall, cols = _build_X(parsed, keys_all, use_score_all, med_all, score_med_all)
    Xs, mean, std = _standardize(Xall)
    w, b = _fit_logreg(Xs, y, l2=l2)
    coef = sorted([{"feature": cols[i], "weight": round(float(w[i]), 4)} for i in range(len(cols))],
                  key=lambda z: -abs(z["weight"]))
    out = {"ok": True, "n": n, "n_eval": int(val.sum()),
           "eval_method": "time_series_walkforward",
           "win_base_rate": round(float(y.mean()) * 100, 1),
           "model_auc": round(model_auc, 4) if model_auc is not None else None,
           "baseline_auc_rule_score": round(base_auc, 4) if base_auc is not None else None,
           "auc_lift": round(model_auc - base_auc, 4) if (model_auc is not None and base_auc is not None) else None,
           "brier": brier, "deciles": deciles, "top_features": coef[:15],
           "n_features": len(cols), "trained_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if persist:
        try:
            from state_store import store
            store.set_meta(META_KEY, {"keys": cols, "mean": mean.tolist(), "std": std.tolist(),
                                      "w": w.tolist(), "b": float(b), "trained_at": out["trained_at"],
                                      "n": n, "model_auc": out["model_auc"],
                                      "baseline_auc": out["baseline_auc_rule_score"]})
            out["persisted"] = True
        except Exception as e:
            out["persisted"] = False; out["persist_error"] = str(e)
    return out


def score_features(feat: dict, score: Optional[float] = None) -> Optional[float]:
    """用已存的模型對一筆特徵算勝率（0~1）。供日後 shadow 並行評分用；無模型回 None。
    注意：此函式不被即時訊號引用，需等 shadow 驗證通過、使用者同意後才接。"""
    try:
        from state_store import store
        m = store.get_meta(META_KEY)
        if not m or not m.get("keys"):
            return None
        keys, mean, std = m["keys"], np.array(m["mean"]), np.array(m["std"])
        w, b = np.array(m["w"]), float(m["b"])
        x = np.zeros(len(keys))
        for j, k in enumerate(keys):
            if k == "score":
                v = _num(score)
            else:
                v = _num((feat or {}).get(k))
            x[j] = v if v is not None else mean[j]      # 缺值用訓練均值（標準化後≈0）
        std = np.where(std < 1e-9, 1.0, std)
        z = (x - mean) / std
        return float(_sigmoid(z @ w + b))
    except Exception as e:
        logger.warning(f"score_features: {e}")
        return None
