"""策略學習・暖啟動勝率模型（第一版，離線訓練、shadow-only）。

使用者要「像 AI 一樣不斷用資料模擬、從既有規則基礎(50-60%)再學」。這支用已累積的
shadow_signals（每筆訊號的特徵 JSON ＋ 實算淨 R）訓練一個「勝率」模型：

- 標籤 y = 1 若 r_multiple > 0（勝），否則 0。用系統已算好的淨 R，不另定義、不編造。
- 特徵 = 訊號產生當下擷取的 features（純數值、覆蓋率≥門檻者）＋ 規則分數 score
  （把現有規則當成其中一個特徵＝暖啟動/先驗，不是從 0 學起）。
- 模型 = numpy 邏輯回歸（可解釋、輕量，不引入 sklearn 等重依賴，守住 Render 記憶體）。
- 評估 = 5-fold 交叉驗證的 out-of-fold AUC，並和「只用規則分數」的基準比，看是否真的有提升；
  另附模型分數分十組的勝率與平均 R（實務增益）。
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
    """讀已結算、有淨 R 的 shadow 列：score / r_multiple / kind / features。"""
    from state_store import store
    import shadow
    shadow.ensure_table()
    with store._conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT score, r_multiple, kind, features FROM shadow_signals "
            "WHERE status='closed' AND r_multiple IS NOT NULL").fetchall()]


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except Exception:
        return None


def build_dataset():
    """回傳 (X, y, keys, score, rmult, per_kind) 或 None（資料不足）。
    X：標準化前的原始特徵矩陣；y：勝(1)/負(0)；score：規則分數；rmult：淨 R。"""
    rows = _rows()
    if len(rows) < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient", "n": len(rows)}
    parsed, ys, scores, rmults, kinds = [], [], [], [], []
    keycount = {}
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
                keycount[k] = keycount.get(k, 0) + 1
        sc = _num(r.get("score"))
        parsed.append((fnum, sc))
        ys.append(1 if rm > 0 else 0)
        scores.append(sc if sc is not None else np.nan)
        rmults.append(rm)
        kinds.append(r.get("kind") or "?")
    n = len(parsed)
    if n < MIN_SAMPLES:
        return {"ok": False, "reason": "insufficient", "n": n}
    # 選覆蓋率夠的特徵鍵（含 score 一律納入當暖啟動先驗）
    keys = sorted([k for k, c in keycount.items() if c / n >= MIN_COVERAGE])
    use_score = sum(1 for _, s in parsed if s is not None) / n >= MIN_COVERAGE
    # 中位數填補缺值
    med = {}
    for k in keys:
        vals = [f.get(k) for f, _ in parsed if f.get(k) is not None]
        med[k] = float(np.median(vals)) if vals else 0.0
    score_med = float(np.nanmedian([s for s in scores if s == s])) if use_score else 0.0
    cols = list(keys) + (["score"] if use_score else [])
    X = np.zeros((n, len(cols)), dtype=float)
    for i, (f, s) in enumerate(parsed):
        for j, k in enumerate(keys):
            X[i, j] = f.get(k, med[k]) if f.get(k) is not None else med[k]
        if use_score:
            X[i, len(keys)] = (s if s is not None else score_med)
    return {"ok": True, "X": X, "y": np.array(ys, dtype=float), "keys": cols,
            "score": np.array(scores, dtype=float), "rmult": np.array(rmults, dtype=float),
            "kinds": kinds, "n": n}


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
    """訓練＋交叉驗證＋和規則分數基準比較；可選擇把最終模型存進 meta。"""
    ds = build_dataset()
    if not ds.get("ok"):
        return {"ok": False, "reason": ds.get("reason", "insufficient"), "n": ds.get("n", 0),
                "min_samples": MIN_SAMPLES}
    X, y, keys = ds["X"], ds["y"], ds["keys"]
    n = len(y)
    rng = np.random.default_rng(42)
    idx = rng.permutation(n)
    folds = np.array_split(idx, k)
    oof = np.full(n, np.nan)
    for f in range(k):
        te = folds[f]; tr = np.concatenate([folds[j] for j in range(k) if j != f])
        Xtr, mean, std = _standardize(X[tr])
        w, b = _fit_logreg(Xtr, y[tr], l2=l2)
        Xte, _, _ = _standardize(X[te], mean, std)
        oof[te] = _sigmoid(Xte @ w + b)
    model_auc = _auc(oof, y)
    base_auc = _auc(ds["score"], y)       # 基準：只用現有規則分數
    # 十分位增益：依模型分數分組看勝率與平均 R
    deciles = []
    order = np.argsort(oof)
    for g in range(10):
        gi = order[g * n // 10:(g + 1) * n // 10]
        if len(gi):
            deciles.append({"decile": g + 1, "n": int(len(gi)),
                            "win_rate": round(float(y[gi].mean()) * 100, 1),
                            "avg_r": round(float(ds["rmult"][gi].mean()), 3)})
    # 最終模型（全資料）＋係數（標準化空間）供解釋與日後 shadow 評分
    Xs, mean, std = _standardize(X)
    w, b = _fit_logreg(Xs, y, l2=l2)
    coef = sorted([{"feature": keys[i], "weight": round(float(w[i]), 4)} for i in range(len(keys))],
                  key=lambda z: -abs(z["weight"]))
    out = {"ok": True, "n": n, "win_base_rate": round(float(y.mean()) * 100, 1),
           "model_auc": round(model_auc, 4) if model_auc is not None else None,
           "baseline_auc_rule_score": round(base_auc, 4) if base_auc is not None else None,
           "auc_lift": round(model_auc - base_auc, 4) if (model_auc is not None and base_auc is not None) else None,
           "deciles": deciles, "top_features": coef[:15],
           "n_features": len(keys), "trained_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if persist:
        try:
            from state_store import store
            store.set_meta(META_KEY, {"keys": keys, "mean": mean.tolist(), "std": std.tolist(),
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
