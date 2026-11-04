"""暖啟動勝率模型 learning_model（合成資料驗證，不連 DB）。

驗證：AUC 計算正確、資料不足時回 insufficient（不硬給結論）、
可辨識的特徵能訓練出 AUC 明顯>0.5 的模型、score_features 能用存下的模型評分。
"""
import json
import numpy as np
import pytest

import learning_model as lm


def test_auc_perfect_and_reversed():
    y = np.array([0, 0, 1, 1])
    assert lm._auc(np.array([0.1, 0.2, 0.8, 0.9]), y) == 1.0
    assert lm._auc(np.array([0.9, 0.8, 0.2, 0.1]), y) == 0.0
    assert lm._auc(np.array([1, 1, 1, 1]), y) == 0.5          # 全同分 → 0.5


def test_insufficient_data(monkeypatch):
    monkeypatch.setattr(lm, "_rows", lambda: [{"score": 1, "r_multiple": 0.5, "kind": "candidate",
                                               "features": "{}"}] * 10)
    out = lm.train_and_eval()
    assert out["ok"] is False and out["reason"] == "insufficient"


def _synth_rows(n=800, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        x = float(rng.standard_normal())                     # 可預測特徵
        noise = float(rng.standard_normal())
        rm = x + 0.3 * noise                                 # 淨 R 由 x 主導 → y=(rm>0) 可學
        score = x * 0.9 + 0.5 * float(rng.standard_normal())  # 規則分數（基準）
        rows.append({"score": score, "r_multiple": rm, "kind": "candidate",
                     "features": json.dumps({"f1": x, "f2": float(rng.standard_normal())})})
    return rows


def test_train_beats_coin_flip_and_reports_baseline(monkeypatch):
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows())
    out = lm.train_and_eval(persist=False)
    assert out["ok"] and out["n"] == 800
    assert out["model_auc"] is not None and out["model_auc"] > 0.75     # 可預測特徵 → 明顯優於 0.5
    assert out["baseline_auc_rule_score"] is not None                   # 有算規則分數基準
    assert len(out["deciles"]) == 10 and out["top_features"]
    # 十分位應單調大致遞增（最高分組勝率 > 最低分組）
    assert out["deciles"][-1]["win_rate"] > out["deciles"][0]["win_rate"]


def test_time_series_walkforward_no_leakage(monkeypatch):
    # ★ F-06：評估須為時間序列 walk-forward，且最早一段只做訓練（不進驗證），
    # 因此被評估的樣本數 n_eval 必定小於總樣本數 n（代表沒有把全部樣本都拿來「自評」）。
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows(n=800))
    out = lm.train_and_eval(persist=False)
    assert out["eval_method"] == "time_series_walkforward"
    assert 0 < out["n_eval"] < out["n"]
    assert out["brier"] is not None and 0.0 <= out["brier"] <= 1.0


def test_persist_and_score_features(monkeypatch):
    saved = {}
    import state_store
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows(seed=1))
    monkeypatch.setattr(state_store.store, "set_meta", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(state_store.store, "get_meta", lambda k: saved.get(k))
    out = lm.train_and_eval(persist=True)
    assert out.get("persisted") is True and lm.META_KEY in saved
    # 用存下的模型評分：高 f1 應給高勝率、低 f1 應給低勝率
    p_hi = lm.score_features({"f1": 2.0, "f2": 0.0}, score=1.8)
    p_lo = lm.score_features({"f1": -2.0, "f2": 0.0}, score=-1.8)
    assert p_hi is not None and p_lo is not None and p_hi > p_lo
