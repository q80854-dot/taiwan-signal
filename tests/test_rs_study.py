"""相對強弱研究工具（research/rs_study.py）：濾網事前寫死、特徵只用訊號日以前的資料、缺漏不猜。"""
from research import rs_study as rs


def _hist(n=300, step=0.5, base=100.0):
    cl = [base + i * step for i in range(n)]
    return {"dates": [f"D{i:04d}" for i in range(n)], "closes": cl, "highs": [c + 1 for c in cl]}


def test_filters_are_predeclared():
    assert [f["id"] for f in rs.FILTERS] == ["R1", "R2", "R3", "R4", "R5", "R6"]
    assert rs.FILTERS[0]["keep"]({}) is None                      # 特徵缺漏：不判定（兩組皆不計）
    assert rs.FILTERS[3]["keep"]({"rank60": 70}) is True and rs.FILTERS[3]["keep"]({"rank60": 69.9}) is False


def test_features_use_only_data_up_to_signal_date():
    idx = _hist(step=0.1)
    strong, weak = _hist(step=1.0), _hist(step=0.0)
    hist = {"S.TW": strong, "W.TW": weak}
    for k in range(40):                                           # 橫斷面母體至少 30 檔才排名
        hist[f"X{k}.TW"] = _hist(step=0.2 + k * 0.001)
    sigs = [{"ticker": "S.TW", "signal_date": "D0260", "r": 1.0}, {"ticker": "W.TW", "signal_date": "D0260", "r": -1.0}]
    f1 = rs.build_features(sigs, hist, idx)
    assert f1[0]["rs60"] > 0 > f1[1]["rs60"] and f1[0]["rank120"] > 90 and f1[1]["rank120"] < 10
    assert f1[0]["near_high"] > 0.99
    # 竄改訊號日「之後」的價格，特徵不得改變
    strong2 = dict(strong, closes=strong["closes"][:261] + [1.0] * 39, highs=strong["highs"][:261] + [9999.0] * 39)
    f2 = rs.build_features(sigs, dict(hist, **{"S.TW": strong2}), idx)
    assert f2[0] == f1[0]


def test_missing_history_gives_none_not_a_guess():
    idx = _hist()
    hist = {"S.TW": _hist(n=300)}
    sigs = [{"ticker": "S.TW", "signal_date": "D0050", "r": 0.1},      # 不足 60／120／250 根
            {"ticker": "NONE.TW", "signal_date": "D0260", "r": 0.1}]   # 沒有這檔的資料
    f = rs.build_features(sigs, hist, idx)
    assert f[0].get("rs120") is None and f[0].get("near_high") is None and f[0]["rs20"] is not None
    assert f[0].get("rank60") is None                                  # 母體不足 30 檔：不排名
    assert f[1] == {}


def test_summary_selects_on_train_and_confirms_on_test():
    import random
    rng = random.Random(3)
    days = [f"20{y}-{m:02d}-{d:02d}" for y in (22, 23, 24, 25) for m in range(1, 13) for d in (5, 15, 25)]
    sigs, feats = [], []
    for d in days:
        for k in range(6):
            strong = k % 2 == 0
            sigs.append({"ticker": f"T{k}", "signal_date": d, "r": rng.gauss(0.3 if strong else -0.4, 0.8)})
            # R2 真的有區別力；R3 只在訓練期有（隨機指派）
            feats.append({"rs60": 1.0 if strong else -1.0, "rs20": rng.choice([-1, 1]), "rs120": rng.choice([-1, 1]),
                          "rank60": rng.uniform(0, 100), "rank120": rng.uniform(0, 100), "near_high": rng.uniform(.5, 1)})
    rep = rs.summarize(sigs, feats)
    assert rep["verdict"]["selected"] == "R2" and rep["verdict"]["result"] == "confirmed"
    assert rep["verdict"]["kept_group_positive_in_test"] is True
    t = rep["filters"]["R2"]["test"]
    assert t["kept_share"] == 50.0 and t["kept_avg_r"] > 0 > t["dropped_avg_r"]
    noise = [dict(f, rs60=rng.choice([-1, 1])) for f in feats]
    assert rs.summarize(sigs, noise)["verdict"]["result"] == "none_selected"
