"""S-P0-06：學習模型的標籤定義正名，並提供 Champion 分批淨 R 目標（2026-11-12）。

原本模型標籤＝r_multiple>0（Research 單次出場），與正式分批出場不是同一個目標。新增
target="champion"：只用已定案（final）的 Champion 分批淨 R，以 Champion 軌出場日為標籤實現日；
暫估（provisional）值不得入訓練。champion 目標只評估、不落地，不覆寫現有 shadow 模型。
"""
import learning_model as lm
import shadow
from state_store import store

_TK = "LMTGT.TW"


def _cleanup():
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (_TK,))


def _insert(bar_date, r_multiple, champion_r, status, champ_exit, prov=None):
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,"
            "status,result,exit_date,r_multiple,champion_r,champion_status,champion_exit_date,champion_r_prov,"
            "features,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bar_date, _TK, "9999", "測試", "candidate", "buy", 80, 100.0, 95.0, 105.0, 110.0, 115.0,
             "closed", "tp1", "2026-10-03", r_multiple, champion_r, status, champ_exit, prov, "{}", bar_date))


def test_champion_target_uses_only_final_rows_and_champion_exit_date():
    _cleanup()
    try:
        _insert("2026-10-01", 0.9, 1.8, "final", "2026-10-09")            # 已定案
        _insert("2026-10-02", 0.9, None, "provisional", None, prov=0.7)   # 仍有部位在場：暫估不得入訓練
        _insert("2026-10-05", 0.9, None, "no_fresh_price", None)          # 無法定案
        mine = [r for r in lm._rows("buy", "champion") if r["bar_date"] in ("2026-10-01", "2026-10-02", "2026-10-05")
                and r.get("features") == "{}"]
        assert [(r["bar_date"], r["r_multiple"], r["exit_date"]) for r in mine] == [("2026-10-01", 1.8, "2026-10-09")]
        # research 目標不變：三筆都在，標籤是單次出場 R、實現日是 Research 的 exit_date
        res = [r for r in lm._rows("buy") if r.get("features") == "{}"
               and r["bar_date"] in ("2026-10-01", "2026-10-02", "2026-10-05")]
        assert len(res) == 3 and all(r["r_multiple"] == 0.9 and r["exit_date"] == "2026-10-03" for r in res)
    finally:
        _cleanup()


def test_targets_are_named_and_unknown_target_rejected():
    assert "單次出場" in lm.TARGETS["research"] and "分批出場" in lm.TARGETS["champion"]
    out = lm.build_dataset(target="whatever")
    assert out["ok"] is False and out["reason"] == "unknown_target"


def test_champion_target_reports_insufficient_honestly_and_never_persists(monkeypatch):
    saved = []
    monkeypatch.setattr(store, "set_meta", lambda k, v: saved.append(k))
    monkeypatch.setattr(lm, "_rows", lambda direction="buy", target="research": [])
    out = lm.train_and_eval(persist=True, target="champion")
    assert out["ok"] is False and out["reason"] == "insufficient" and out["target"] == "champion"
    assert "分批出場" in out["label_definition"] and saved == []      # 樣本不足不硬給結論、也不落地
