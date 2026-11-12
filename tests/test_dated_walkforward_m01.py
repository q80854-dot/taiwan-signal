"""M01/M02 第一刀（第三階段審查）：日期感知 purged walk-forward（對照用，不取代既有路徑）。

- 按決策日切分：同一 bar_date 不跨 train/val；訓練日全早於驗證日。
- Purge：訓練樣本標籤實現日（end_date）不得 ≥ 驗證首日 − embargo。
- M02 隔離：end_known=False（exit_date 缺）不進訓練段。
- protocol 逐折記錄日期範圍與刪除數。
"""
import numpy as np
import learning_model as LM


def _mk(n_days=12, per_day=10, late_idx=None, unknown_idx=None):
    parsed, y, sc, rm, bd, ed, ek = [], [], [], [], [], [], []
    idx = 0
    for d in range(n_days):
        day = f"2026-10-{d+1:02d}"
        for j in range(per_day):
            f1 = (j - per_day / 2) + (0.3 if d % 2 else -0.3)
            parsed.append(({"f1": f1, "f2": float(d)}, 60.0 + j))
            y.append(1.0 if f1 > 0 else 0.0)
            sc.append(60.0 + j)
            rm.append(1.0 if f1 > 0 else -1.0)
            bd.append(day)
            # 正常：兩天後實現
            ed.append(f"2026-10-{min(n_days, d+3):02d}")
            ek.append(True)
            idx += 1
    if late_idx is not None:
        ed[late_idx] = f"2026-10-{n_days:02d}"      # 很晚才成熟 → 各折都該被 purge
    if unknown_idx is not None:
        ek[unknown_idx] = False                      # 實現日不可靠 → 隔離
    return parsed, np.array(y), np.array(sc), np.array(rm), bd, ed, ek


def test_days_never_straddle_and_train_before_val():
    parsed, y, sc, rm, bd, ed, ek = _mk()
    r = LM._walkforward_eval_dated(parsed, y, sc, rm, bd, ed, ek, k=3, embargo_days=0)
    assert r["ok"] is True
    for f in r["protocol"]["folds"]:
        assert f["train_days"][1] < f["val_days"][0]          # 訓練日全早於驗證日（ISO 字串可比）


def test_late_maturity_sample_is_purged():
    parsed, y, sc, rm, bd, ed, ek = _mk(late_idx=0)           # 第 1 天的樣本、最後一天才實現
    r = LM._walkforward_eval_dated(parsed, y, sc, rm, bd, ed, ek, k=3, embargo_days=0)
    assert all(f["n_purged"] >= 1 for f in r["protocol"]["folds"])


def test_unknown_end_date_quarantined_from_training():
    parsed, y, sc, rm, bd, ed, ek = _mk(unknown_idx=0)
    r = LM._walkforward_eval_dated(parsed, y, sc, rm, bd, ed, ek, k=3, embargo_days=0)
    assert all(f["n_quarantined"] >= 1 for f in r["protocol"]["folds"])
    # 有效訓練數 = 原始 − purged − quarantined
    for f in r["protocol"]["folds"]:
        assert f["n_train"] == f["n_train_raw"] - f["n_purged"] - f["n_quarantined"]


def test_metrics_computed_on_separable_data():
    parsed, y, sc, rm, bd, ed, ek = _mk()
    r = LM._walkforward_eval_dated(parsed, y, sc, rm, bd, ed, ek, k=3, embargo_days=0)
    assert r["n_eval"] > 0 and r["model_auc"] is not None
    assert r["model_auc"] > 0.9                               # f1 完全可分


def test_too_few_days_honest():
    parsed, y, sc, rm, bd, ed, ek = _mk(n_days=3)
    r = LM._walkforward_eval_dated(parsed, y, sc, rm, bd, ed, ek, k=5, embargo_days=0)
    assert r["ok"] is False and r["reason"] == "too_few_days"


# ── 2026-11-12 M01 口徑切換（使用者核准「一和二可以做」）：dated 成為 train_and_eval 主口徑 ──

def _synth_dated_rows(n_days=40, per_day=20, seed=1):
    import json as _json
    from datetime import date, timedelta
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        day = (date(2026, 8, 1) + timedelta(days=d)).isoformat()
        exitd = (date(2026, 8, 1) + timedelta(days=d + 2)).isoformat()
        for _ in range(per_day):
            x = float(rng.standard_normal())
            rm = x + 0.3 * float(rng.standard_normal())
            rows.append({"score": x * 0.9 + 0.5 * float(rng.standard_normal()),
                         "r_multiple": rm, "kind": "candidate",
                         "features": _json.dumps({"f1": x}),
                         "bar_date": day, "exit_date": exitd})
    return rows


def test_train_and_eval_dated_is_primary(monkeypatch):
    monkeypatch.setattr(LM, "_rows", lambda direction="buy": _synth_dated_rows())
    out = LM.train_and_eval(persist=False)
    assert out["ok"] and out["eval_method"] == "dated_purged_walkforward"
    assert out["dated"]["ok"] is True
    assert out["model_auc"] == out["dated"]["model_auc"]            # 主數字＝dated 口徑
    assert out["brier"] == out["dated"]["brier"]
    assert len(out["deciles"]) == 10                                 # dated 也有十分位
    lg = out["legacy_count_based"]                                   # 舊口徑降為對照、仍誠實保留
    assert lg["model_auc"] is not None and "洩漏" in lg["note"]
    assert "主口徑＝dated" in out["dated_note"]


def test_train_and_eval_falls_back_honestly_without_dates(monkeypatch):
    import json as _json
    rng = np.random.default_rng(0)
    rows = [{"score": float(rng.standard_normal()), "r_multiple": float(rng.standard_normal()),
             "kind": "candidate", "features": _json.dumps({"f1": float(rng.standard_normal())})}
            for _ in range(800)]                                     # 無 bar_date → dated 不可用
    monkeypatch.setattr(LM, "_rows", lambda direction="buy": rows)
    out = LM.train_and_eval(persist=False)
    assert out["ok"] and out["eval_method"] == "time_series_walkforward"   # 誠實退回、不假裝已切換
    assert out["dated"].get("ok") is not True
    assert "退回" in out["dated_note"] and "legacy_count_based" not in out
