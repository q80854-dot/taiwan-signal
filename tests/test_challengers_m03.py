"""M03 第一刀（第三階段審查）：內層 dev 折選模、封存段只評一次（並列對照）。

- 選模（inner_dev_rank）只用 dev 段日期感知 OOF，不接觸封存段。
- 封存段只評選中者＋規則基準＋現任 champion 設定（同批重評），各一次。
- 封存邊界按日對齊：同一 bar_date 不跨 dev/封存。
- 既有 entries/recommendation 口徑不變（切換需使用者核准）；attempts 記錄查看次數。
"""
import datetime as dt
import json

import numpy as np

import learning_model as lm
import state_store


class _MemStore:
    def __init__(self): self.box = {}
    def read_meta(self, k): return ("ok", self.box[k]) if k in self.box else ("missing", None)
    def get_meta(self, k, default=None): return self.box.get(k, default)
    def set_meta(self, k, v): self.box[k] = v; return True


def _rows_dated(n=800, seed=7, days=120):
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


def _run(monkeypatch, mem=None):
    monkeypatch.setattr(state_store, "store", mem or _MemStore())
    monkeypatch.setattr(lm, "_rows", lambda direction="buy": _rows_dated())
    return lm.run_challengers()


def test_m03_selection_uses_only_dev(monkeypatch):
    out = _run(monkeypatch)
    m = out["m03"]
    assert m["ok"] is True
    # 內層排名只有 dev 指標、沒有封存段數字
    for e in m["inner_dev_rank"]:
        assert "dev_auc" in e and "holdout_auc" not in e and "auc" not in e
    ranks = [e["dev_rank"] for e in m["inner_dev_rank"]]
    assert ranks == list(range(1, len(ranks) + 1))
    assert m["selected_id"] == m["inner_dev_rank"][0]["id"]


def test_m03_sealed_evaluated_once_for_three_parties_only(monkeypatch):
    out = _run(monkeypatch)
    s = out["m03"]["sealed_once"]
    assert set(s.keys()) == {"selected", "baseline_rule", "champion_same_batch"}
    assert s["selected"] is not None and s["selected"]["auc"] is not None
    assert s["baseline_rule"] is not None
    assert s["champion_same_batch"] is None              # 無現任 champion → 不硬比舊期間數字
    # 晉升證據：校準（brier）、淨 R、樣本量
    for kk in ("brier", "sel_net_r", "sel_n", "n_train"):
        assert kk in s["selected"]
    assert "promote" in out["m03"]["recommendation"]


def test_m03_day_aligned_boundary(monkeypatch):
    out = _run(monkeypatch)
    p = out["m03"]["protocol"]
    assert p["split"] == "day_aligned_holdout" and p["selection"] == "inner_dev_oof_only"
    assert p["dev_days"] + p["holdout_days"] == 120
    assert p["n_dev"] + p["n_holdout"] + p["excluded_undated"] == out["n"]


def test_m03_sidecar_keeps_existing_contract_and_counts_attempts(monkeypatch):
    mem = _MemStore()
    out = _run(monkeypatch, mem)
    # 既有口徑不變：entries 仍按 holdout 排名（切換需核准）、recommendation 原樣
    assert len(out["entries"]) == len(lm._COMPETITION_CONFIGS)
    aucs = [e["holdout_auc"] for e in out["entries"] if e["holdout_auc"] is not None]
    assert aucs == sorted(aucs, reverse=True)
    assert "m03_note" in out and out["m03"]["holdout_note"]
    assert out["m03"]["attempts"] == 1
    out2 = lm.run_challengers()
    assert out2["m03"]["attempts"] == 2                   # 查看次數誠實累計


def test_m03_too_few_days_honest(monkeypatch):
    monkeypatch.setattr(state_store, "store", _MemStore())
    monkeypatch.setattr(lm, "_rows", lambda direction="buy": _rows_dated(days=5))
    out = lm.run_challengers()
    assert out["m03"]["ok"] is False and out["m03"]["reason"] == "too_few_days"
