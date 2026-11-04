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


class _MemStore:
    def __init__(self): self.box = {}
    def read_meta(self, k): return ("ok", self.box[k]) if k in self.box else ("missing", None)
    def get_meta(self, k, default=None): return self.box.get(k, default)
    def set_meta(self, k, v): self.box[k] = v; return True


def test_competition_ranks_and_rule_baseline(monkeypatch):
    # Phase 2：多模型競賽——同一批資料跑多種設定、產出排名；規則基準也是其中一個參賽者
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows(n=800))
    out = lm.run_competition()
    assert out["ok"] and len(out["entries"]) == len(lm._COMPETITION_CONFIGS)
    ranks = [e["rank"] for e in out["entries"]]
    assert ranks == sorted(ranks) == list(range(1, len(ranks) + 1))   # rank 連續遞增
    aucs = [e["model_auc"] for e in out["entries"]]
    assert aucs[0] >= aucs[-1]                                        # 第一名不低於最後一名
    assert out["baseline_auc_rule_score"] is not None
    assert any(e["id"] == "rule_only" for e in out["entries"])        # 規則基準有參賽


def test_model_version_history(monkeypatch):
    # Phase 2：模型版本管理——每次 persist 訓練就追加版本，可列出與排行
    import state_store
    mem = _MemStore()
    monkeypatch.setattr(state_store, "store", mem)
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows(n=800))
    lm.train_and_eval(persist=True)
    lm.train_and_eval(persist=True)
    listing = lm.list_model_versions()
    assert listing["ok"] and listing["count"] == 2
    assert listing["versions"][0]["version"] == 1 and listing["versions"][1]["version"] == 2
    # 排行榜依 AUC 由高到低
    lb = listing["leaderboard"]
    assert all(lb[i]["model_auc"] >= lb[i + 1]["model_auc"] for i in range(len(lb) - 1))


def test_explain_signal_contributions(monkeypatch):
    # Phase 2：AI 決策解釋——高 f1＋高 score 應給高勝率，且 f1 貢獻為正
    import state_store
    mem = _MemStore()
    monkeypatch.setattr(state_store, "store", mem)
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows(n=800))
    lm.train_and_eval(persist=True)
    ex = lm.explain_signal({"f1": 2.0, "f2": 0.0}, score=1.8)
    assert ex is not None and 0.0 <= ex["prob"] <= 1.0 and ex["prob"] > 0.5
    f1 = next(c for c in ex["contributions"] if c["feature"] == "f1")
    assert f1["contribution"] > 0


def _synth_rows_dated(n=800, seed=5, days=120):
    import datetime as dt
    rng = np.random.default_rng(seed)
    base = dt.date(2026, 6, 1)
    rows = []
    for i in range(n):
        x = float(rng.standard_normal())
        rm = x + 0.3 * float(rng.standard_normal())
        off = int(i * days / n)
        rows.append({"bar_date": (base + dt.timedelta(days=off)).isoformat(),
                     "exit_date": (base + dt.timedelta(days=off + 3)).isoformat(),
                     "score": x * 0.9 + 0.5 * float(rng.standard_normal()),
                     "r_multiple": rm, "kind": "candidate", "features": json.dumps({"f1": x})})
    return rows


def test_challengers_insufficient_blocks_promote(monkeypatch):
    # B：單一日期（無行情多樣性）→ 資料關卡不過，不建議晉升
    import state_store
    monkeypatch.setattr(state_store, "store", _MemStore())
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows())      # 無 bar_date → holdout 交易日=0
    out = lm.run_challengers()
    assert out["ok"] and out["gates_ok"] is False
    assert out["recommendation"]["promote"] is None


def test_challengers_with_dates_ranks_and_gates(monkeypatch):
    # B：跨多個交易日 → 通過資料關卡、依 holdout AUC 排名
    import state_store
    monkeypatch.setattr(state_store, "store", _MemStore())
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows_dated(n=800, days=120))
    out = lm.run_challengers()
    assert out["ok"] and out["gates_ok"] is True
    assert out["n_holdout"] >= lm.MIN_HOLDOUT and out["holdout_days"] >= lm.MIN_HOLDOUT_DAYS
    assert len(out["entries"]) == len(lm._COMPETITION_CONFIGS)
    ranks = [e["rank"] for e in out["entries"]]
    assert ranks == list(range(1, len(ranks) + 1))
    aucs = [e["holdout_auc"] for e in out["entries"] if e["holdout_auc"] is not None]
    assert aucs == sorted(aucs, reverse=True)      # holdout AUC 由高到低
    assert "promote" in out["recommendation"]


def test_promote_refused_when_gates_fail(monkeypatch):
    # B：資料關卡未過時，手動晉升也會被拒（守「不要資訊錯誤」）
    import state_store
    monkeypatch.setattr(state_store, "store", _MemStore())
    monkeypatch.setattr(lm, "_rows", lambda: _synth_rows())
    out = lm.promote_champion("rule_feats_l2_1")
    assert out["ok"] is False and "關卡" in out["reason"]


def test_month_feature_excluded(monkeypatch):
    # ★ 2026-11-04：month 屬薄資料下的「背時間」過擬合來源，須被排除，不得進入模型特徵
    assert "month" in lm._EXCLUDE_KEYS
    rng = np.random.default_rng(3)

    def _rows():
        rows = []
        for i in range(800):
            x = float(rng.standard_normal())
            rows.append({"bar_date": "2026-10-01", "score": x,
                         "r_multiple": x + 0.3 * float(rng.standard_normal()),
                         "kind": "candidate",
                         "features": json.dumps({"f1": x, "month": 10, "dow": i % 5})})
        return rows
    monkeypatch.setattr(lm, "_rows", _rows)
    ds = lm.build_dataset()
    assert ds["ok"]
    assert all("month" not in f for f, _ in ds["parsed"])          # 不在特徵集
    out = lm.train_and_eval(persist=False)
    assert all(t["feature"] != "month" for t in out["top_features"])  # 不在模型係數


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
