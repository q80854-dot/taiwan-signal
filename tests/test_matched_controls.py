"""S-P1-04 配對對照組、S-P1-05 二維拔靴穩健性（2026-11-12）。只記錄與評估，不影響正式訊號。"""
import json

import experiments as ex
import shadow


def _p(t, sector, size, turnover):
    return {"ticker": t, "code": t[:4], "name": t, "kind": "matched", "direction": "buy", "bar_date": "2031-05-02",
            "entry": 100.0, "stop": 95.0, "tp1": 107.5, "tp2": 112.5, "tp3": 120.0, "size_cat": size,
            "sector": sector, "turnover": turnover, "created_at": "x", "score": None}


def test_select_matched_prefers_same_sector_and_size_then_nearest_turnover():
    pool = {p["ticker"]: p for p in [
        _p("A.TW", "半導體業", "中型股", 1e6), _p("B.TW", "半導體業", "中型股", 3e6),
        _p("C.TW", "半導體業", "中型股", 9e6), _p("D.TW", "半導體業", "中型股", 1.1e6),
        _p("E.TW", "航運業", "中型股", 1e6), _p("F.TW", "半導體業", "大型股", 1e6),
        _p("CAND.TW", "半導體業", "中型股", 1e6)]}
    cands = [{"ticker": "CAND.TW", "direction": "buy", "sector": "半導體業", "size_cat": "中型股", "turnover": 1e6}]
    m = shadow._select_matched(cands, pool)
    assert sorted(m) == ["A.TW", "B.TW", "D.TW"]                 # 同產業同規模、成交金額最接近的 3 檔；候選自己不入選
    assert all(v["level"] == "sector_size" and v["match_for"] == ["CAND.TW"] for v in m.values())


def test_select_matched_falls_back_to_size_and_skips_unknown_sector_and_shorts():
    pool = {p["ticker"]: p for p in [_p("E.TW", "航運業", "小型股", 1e6), _p("G.TW", "其他", "小型股", 2e6),
                                       _p("H.TW", "", "中型股", 1e6)]}
    cands = [{"ticker": "X.TW", "direction": "buy", "sector": "其他", "size_cat": "小型股", "turnover": 1e6},
             {"ticker": "Y.TW", "direction": "sell", "sector": "航運業", "size_cat": "小型股", "turnover": 1e6},
             {"ticker": "Z.TW", "direction": "buy", "sector": "航運業", "size_cat": "小型股", "turnover": None}]
    m = shadow._select_matched(cands, pool)
    assert sorted(m) == ["E.TW", "G.TW"] and all(v["level"] == "size" for v in m.values())   # 「其他」不當產業
    assert all(v["match_for"] == ["X.TW"] for v in m.values())   # 放空、缺成交金額者不配對


def test_flush_controls_tags_overlap_and_adds_matched_rows(monkeypatch):
    got = {}
    monkeypatch.setattr(shadow, "_insert", lambda rows: got.setdefault("rows", rows) and len(rows))
    monkeypatch.setattr(shadow, "_fund_features", lambda code, cache: {})
    rnd = dict(_p("A.TW", "半導體業", "中型股", 1e6), kind="control", features={"pre_score": 40})
    rnd.pop("turnover")
    run = {"controls": [rnd], "fund": {}, "stats": {"ctrl_made": 0, "ctrl_saved": 0, "secs_capture": 0.0},
           "matched": {"A.TW": {"row": _p("A.TW", "半導體業", "中型股", 1e6), "match_for": ["C1.TW"], "level": "sector_size"},
                       "B.TW": {"row": _p("B.TW", "半導體業", "中型股", 2e6), "match_for": ["C1.TW", "C2.TW"], "level": "size"}}}
    monkeypatch.setattr(shadow, "_run", run)
    shadow.flush_controls([])
    rows = {r["ticker"]: r for r in got["rows"]}
    assert rows["A.TW"]["kind"] == "control" and rows["A.TW"]["features"]["match_for"] == ["C1.TW"]   # 重疊：只加標記
    assert rows["B.TW"]["kind"] == "matched" and rows["B.TW"]["features"]["match_for"] == ["C1.TW", "C2.TW"]
    assert "turnover" not in rows["B.TW"] and rows["B.TW"]["features"]["turnover_m"] == 2000.0


def _row(day, t, kind, r20, match_for=None, direction="buy"):
    f = {"match_for": match_for} if match_for else {}
    return {"bar_date": day, "ticker": t, "kind": kind, "direction": direction, "entry": 100.0, "stop": 95.0,
            "gap_pct": 0.0, "r10": None, "r20": r20, "features": json.dumps(f)}


def test_matched_report_uses_percent_returns_and_separates_benchmarks():
    rows = []
    days = [f"2031-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(30)]
    for i, d in enumerate(days):
        for j in range(3):
            c = f"C{i}_{j}.TW"
            rows.append(_row(d, c, "candidate", 0.4))                     # +0.4R × 5 / 100 = +2%
            rows.append(_row(d, f"M{i}_{j}.TW", "matched", 0.2, [c]))     # +1%
        rows.append(_row(d, f"R{i}.TW", "control", 0.0))                  # 0%
    out = shadow.matched_control_report(rows)
    assert out["vs_matched"]["n"] == 90 and out["vs_matched"]["mean_pct"] == 1.0
    assert out["vs_random_controls"]["mean_pct"] == 2.0                   # 隨機基準高估（同 7 年重播的發現方向）
    lo, hi = out["vs_matched"]["ci95_twoway"]
    assert lo is not None and lo <= 1.0 <= hi
    assert shadow._pct_ret({"entry": 100, "stop": 95, "gap_pct": 2.0, "r20": 1.0}) == 5 / 102 * 100
    assert shadow._pct_ret({"entry": 100, "stop": 95, "r20": None}) is None


def test_twoway_ci_is_wider_when_one_stock_drives_the_result():
    days = [f"2031-01-{d:02d}" for d in range(1, 29)]
    obs_spread = [(d, f"T{i}.TW", 1.0 if (k + i) % 2 else -0.5) for k, d in enumerate(days) for i in range(10)]
    obs_one = [(d, "BIG.TW" if i == 0 else f"T{i}.TW", 6.0 if i == 0 else -0.5) for d in days for i in range(10)]
    lo1, hi1 = ex._twoway_ci(obs_spread)
    lo2, hi2 = ex._twoway_ci(obs_one)
    assert (hi2 - lo2) > 3 * (hi1 - lo1)       # 同樣的平均，若由單一股票撐起來：區間大幅變寬（日分群看不出這件事）
    assert ex._twoway_ci(obs_one[:3]) == (None, None)                     # 群數不足不算


def test_verdict_is_fragile_when_only_one_stock_drives_a_blocked_rule_result():
    days = [f"2027-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(45)]
    rows = []
    for d in days:
        for j in range(6):
            t = "BIG.TW" if j == 0 else f"T{j}.TW"
            rows.append({"bar_date": d, "ticker": t, "kind": "rejected", "direction": "sell", "status": "closed",
                         "champion_r": 2.4 if j == 0 else 0.0, "champion_status": "final", "f": {"reject": "short_off"}})
    o = ex.evaluate_blocked("E04", rows)["oos"]
    assert o["avg_r_ci95"][0] > ex.MIN_EFFECT_R                           # 只按日分群：看起來成立
    assert o["avg_r_ci95_twoway"][0] <= 0 and o["verdict"] == "fragile"   # 全靠同一檔：判為不穩健
    spread = [dict(r, ticker=f"S{i}.TW", champion_r=0.4 + (0.05 if i % 2 else -0.05)) for i, r in enumerate(rows)]
    assert ex.evaluate_blocked("E04", spread)["oos"]["verdict"] == "supported"
