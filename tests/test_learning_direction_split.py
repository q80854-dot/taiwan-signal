"""多空標籤拆分（規格 §4.8/7.1）：模型訓練樣本只取單一方向的 candidate，
不混 sell / rejected / control（它們分布與選擇機制不同）。
_rows 不回 ticker，故以 distinct score 辨識哪些列被取回。"""
import json
import learning_model as lm


def _insert(store, ticker, kind, direction, score):
    import shadow
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (ticker,))
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,kind,direction,score,entry,stop,"
            "status,result,r_multiple,features,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-11-08", ticker, kind, direction, score, 600, 570, "closed", "tp1", 1.0,
             json.dumps({"adx_value": 30}), "2026-11-08T00:00:00"))


def test_rows_only_buy_candidates():
    from state_store import store
    _insert(store, "DIRBUY.TW", "candidate", "buy", 801)
    _insert(store, "DIRSELL.TW", "candidate", "sell", 802)      # 空方：排除
    _insert(store, "DIRREJ.TW", "rejected", "buy", 803)          # 被擋：排除
    _insert(store, "DIRCTL.TW", "control", "buy", 804)           # 對照：排除
    scores = {r["score"] for r in lm._rows("buy")}
    assert 801 in scores                                         # buy candidate 取回
    assert 802 not in scores and 803 not in scores and 804 not in scores


def test_rows_direction_sell_filter():
    from state_store import store
    _insert(store, "SBUY.TW", "candidate", "buy", 811)
    _insert(store, "SSELL.TW", "candidate", "sell", 812)
    scores = {r["score"] for r in lm._rows("sell")}
    assert 812 in scores and 811 not in scores


def test_build_dataset_tags_direction(monkeypatch):
    # 不足樣本時 build_dataset 仍回報 direction 標記
    monkeypatch.setattr(lm, "_rows", lambda direction="buy": [])
    ds = lm.build_dataset("buy")
    assert ds["ok"] is False and ds["direction"] == "buy"
    ds2 = lm.build_dataset("sell")
    assert ds2["direction"] == "sell"
