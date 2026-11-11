"""因子 ablation（§三 3.3，2026-11-11）：逐一拿掉單一因子重訓，看樣本外淨期望 R 是否變差。

驗證：真正有預測力的因子 delta_net_r 明顯為正且大於雜訊因子；樣本不足時誠實回報 insufficient。
walk-forward（擴張視窗、per-fold fit）由 learning_model 既有核心負責；此處用合成資料驗證 ablation 邏輯。
"""
import random
import numpy as np
import learning_model as LM


def _ds(n=300, seed=0):
    rng = random.Random(seed)
    parsed, ys, scores, rmults = [], [], [], []
    for _ in range(n):
        rm = rng.gauss(0.1, 1.0)
        good = rm + rng.gauss(0, 0.3)        # 與淨 R 同向（真預測力）
        noise = rng.gauss(0, 1.0)            # 與結果無關
        parsed.append(({"good": good, "noise": noise}, None))
        ys.append(1 if rm > 0 else 0)
        scores.append(np.nan)
        rmults.append(rm)
    return {"ok": True, "parsed": parsed, "y": np.array(ys, float),
            "score": np.array(scores, float), "rmult": np.array(rmults, float),
            "n": n, "direction": "buy"}


def test_ablation_detects_predictive_factor(monkeypatch):
    monkeypatch.setattr(LM, "build_dataset", lambda direction="buy": _ds())
    rep = LM.factor_ablation(direction="buy", k=5, l2=1.0)
    assert rep["ok"] is True
    facs = {f["factor"]: f for f in rep["factors"]}
    assert "good" in facs and "noise" in facs
    assert rep["baseline"]["oos_sel_net_r"] is not None
    assert facs["good"]["delta_net_r"] is not None
    # 真預測因子：拿掉它樣本外淨期望 R 明顯變差 → delta>0，且遠大於雜訊因子
    assert facs["good"]["delta_net_r"] > 0
    assert facs["good"]["delta_net_r"] > facs["noise"]["delta_net_r"]
    # 排序：good 應排在 noise 前（delta 由大到小）
    order = [f["factor"] for f in rep["factors"]]
    assert order.index("good") < order.index("noise")


def test_ablation_insufficient(monkeypatch):
    monkeypatch.setattr(LM, "build_dataset",
                        lambda direction="buy": {"ok": False, "reason": "insufficient", "n": 12})
    rep = LM.factor_ablation(direction="buy")
    assert rep["ok"] is False and rep["reason"] == "insufficient" and rep["n"] == 12


def test_walkforward_exclude_drops_key():
    # 直接驗證 exclude 會讓最終模型欄位不含被排除的因子。
    ds = _ds(n=120, seed=1)
    out = LM._walkforward_eval(ds["parsed"], ds["y"], ds["score"], ds["rmult"],
                               k=4, l2=1.0, mode="all", exclude=["good"])
    assert "good" not in out["cols"]
