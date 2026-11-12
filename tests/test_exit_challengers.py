"""出場 Challenger（影子平行追蹤，實驗 E07／E08）與新濾網實驗 E09／E10。只記錄與評估，不影響正式訊號。"""
import json

import execution
import experiments as ex
import shadow


def _bars(seq, month=3, start_day=1):
    out = []
    for i, (o, h, l, c) in enumerate(seq):
        d = start_day + i
        out.append({"bar_date": f"2026-{month + (d - 1) // 28:02d}-{(d - 1) % 28 + 1:02d}", "open": o, "high": h,
                    "low": l, "close": c})
    return out


def _row(**k):
    r = {"bar_date": "2026-03-01", "ticker": "9999.TW", "direction": "buy", "entry": 100.0, "stop": 95.0,
         "tp1": 107.5, "tp2": 112.5, "tp3": 120.0, "features": json.dumps({"sl_atr": 2.0})}
    r.update(k)
    return r


def test_challengers_are_fixed_and_not_the_production_exit():
    assert set(execution.EXIT_CHALLENGERS) == {"hold60", "trail3"}
    assert execution.EXIT_CHALLENGERS["hold60"] == {"name": execution.EXIT_CHALLENGERS["hold60"]["name"],
                                                    "expire": 60, "ladder": True}
    t = execution.EXIT_CHALLENGERS["trail3"]
    assert (t["expire"], t["ladder"], t["trail_atr"], t["trail_after"]) == (60, False, 3.0, "entry")
    import config
    assert config.CIRCUIT_BREAKER["signal_expire_days"] == 15           # 正式出場未改


def test_tracks_are_final_only_when_the_position_really_exited():
    # 訊號日 3/1；第 1 根（3/2）進場後一路持平：兩個方案都還沒出場、也還沒到 60 天 → 不存數字
    flat = _bars([(100, 100, 100, 100)] + [(100, 101, 99, 100)] * 10)
    from datetime import datetime
    t = shadow._alt_exit_tracks(_row(), flat, today=datetime(2026, 3, 20))
    assert t == {"hold60": {"status": "provisional", "r": None}, "trail3": {"status": "provisional", "r": None}}
    # 同樣的資料但今天已超過 60 天＋寬限：K 棒在到期前就停更 → no_fresh_price，不以舊收盤當到期價
    t = shadow._alt_exit_tracks(_row(), flat, today=datetime(2026, 6, 1))
    assert t["hold60"]["status"] == "no_fresh_price" and t["trail3"]["status"] == "no_fresh_price"
    # 跌破停損：兩個方案都定案，約 −1R（再扣成本）
    stop = _bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 94, 94.5)])
    t = shadow._alt_exit_tracks(_row(), stop, today=datetime(2026, 3, 20))
    for k in ("hold60", "trail3"):
        assert t[k]["status"] == "final" and -1.2 < t[k]["r"] < -1.0 and t[k]["exit_date"] == "2026-03-03"


def test_trailing_track_needs_recorded_atr_and_does_not_guess():
    stop = _bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 100, 94, 94.5)])
    t = shadow._alt_exit_tracks(_row(features=json.dumps({})), stop)
    assert t["trail3"] == {"status": "no_atr", "r": None} and t["hold60"]["status"] == "final"


def test_hold60_and_trail3_differ_from_each_other_on_a_runner():
    # 一路上漲後回落：hold60 分批停利於 107.5／112.5／120；trail3 不停利、最高 130 後跌破 130−7.5=122.5 才出場
    seq = [(100, 100, 100, 100), (100, 108, 99.5, 107), (107, 113, 106, 112), (112, 121, 111, 120),
           (120, 130, 119, 129), (129, 129.5, 121, 122)]
    from datetime import datetime
    t = shadow._alt_exit_tracks(_row(), _bars(seq), today=datetime(2026, 3, 20))
    assert t["hold60"]["status"] == "final" and 2.4 < t["hold60"]["r"] < 2.7        # (7.5+12.5+20)/3/5 ≈ 2.67，再扣成本
    assert t["trail3"]["status"] == "final" and 4.2 < t["trail3"]["r"] < 4.5        # (122.5−100)/5 = 4.5 再扣成本


def _er(day, alt_r, champ_r, track="trail3", status="final", kind="candidate", direction="buy"):
    return {"bar_date": day, "kind": kind, "direction": direction, "status": "closed", "champion_r": champ_r,
            "champion_status": "final", "f": {}, "alt": {track: {"status": status, "r": alt_r}}}


def _days(n):
    return [f"2027-{1 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(n)]


def test_exit_experiments_are_preregistered_with_tail_check():
    for e in ex.ALT_EXIT_REGISTRY:
        doc = open(e["doc"], encoding="utf-8").read()
        assert f"id: {e['id']}" in doc and f"track: {e['track']}" in doc and e["track"] in execution.EXIT_CHALLENGERS
        assert f"min_oos_days: {ex.MIN_OOS_DAYS}" in doc and "縮尾" in doc and "size = 10" in doc
    assert [e["id"] for e in ex.ALT_EXIT_REGISTRY] == ["E07", "E08"]


def test_exit_experiment_verdicts():
    days = _days(45)
    steady = [_er(d, 0.3 + (0.05 if j % 2 else -0.05), 0.0) for d in days for j in range(6)]
    o = ex.evaluate_alt_exit("E07", steady)["oos"]
    assert o["n_pairs"] == 270 and o["verdict"] == "supported" and "不自動採用" in o["verdict_note"]
    heavy = [_er(d, 30.0 if (j == 0 and i % 9 == 0) else -0.4, 0.0) for i, d in enumerate(days) for j in range(6)]
    o = ex.evaluate_alt_exit("E07", heavy)["oos"]
    assert o["diff"] > 0.05 and o["diff_winsorized"] < 0 and o["verdict"] in ("tail_dependent", "inconclusive")
    worse = [_er(d, -0.3 + (0.05 if j % 2 else -0.05), 0.0) for d in days for j in range(6)]
    assert ex.evaluate_alt_exit("E08", [dict(r, alt={"hold60": r["alt"]["trail3"]}) for r in worse])["oos"]["verdict"] == "not_supported"
    assert ex.evaluate_alt_exit("E07", steady[:30])["oos"]["verdict"] == "insufficient"
    # 方案尚未出場、放空、被擋下的假想單都不納入成對比較
    noise = [_er("2027-01-01", 9.0, 0.0, status="provisional"), _er("2027-01-01", 9.0, 0.0, direction="sell"),
             _er("2027-01-01", 9.0, 0.0, kind="rejected")]
    o = ex.evaluate_alt_exit("E07", steady + noise)["oos"]
    assert o["n_pairs"] == 270 and o["n_alt_not_final"] == 1
    old = [_er("2026-10-01", 5.0, 0.0)] * 300
    assert ex.evaluate_alt_exit("E07", old)["exploratory"]["verdict"] == "exploratory_only"


def test_tail_dependent_verdict_when_mean_is_significant_but_winsorized_is_not():
    days = _days(45)
    # 每天 6 筆：1 筆 +12R、5 筆 −0.6R → 平均差 +1.5R（顯著），縮尾後 (3 − 5×0.6)/6 = 0 → 靠極端值
    rows = [_er(d, 12.0 if j == 0 else -0.6, 0.0) for d in days for j in range(6)]
    o = ex.evaluate_alt_exit("E07", rows)["oos"]
    assert o["diff_ci95"][0] > ex.MIN_EFFECT_R and o["diff_winsorized_ci95"][0] <= 0
    assert o["verdict"] == "tail_dependent" and "極少數" in o["verdict_note"]


def test_new_filter_experiments_keep_rules():
    e9 = next(e for e in ex.REGISTRY if e["id"] == "E09")
    assert e9["keep"]({"dist_hi52": -10}) is True and e9["keep"]({"dist_hi52": -10.01}) is False
    assert e9["keep"]({}) is None
    e10 = next(e for e in ex.REGISTRY if e["id"] == "E10")
    assert e10["keep"]({"prior_blocked": "none"}) is True and e10["keep"]({"prior_blocked": "gate_weekly_long"}) is False
    assert e10["keep"]({}) is None                              # 11-13 以前的列沒有這個特徵：兩組皆不計


def _ins(ticker, day, kind, reason=None):
    from state_store import store
    f = {"reject": reason} if reason else {}
    with store._conn() as conn:
        conn.execute("INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,status,"
                     "features,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (day, ticker, "9999", "測試", kind, "buy", 70, 100.0, 95.0, "pending",
                      json.dumps(f, ensure_ascii=False), day))


def test_prior_blocked_feature_looks_back_15_days_only_at_stopped_classes():
    from state_store import store
    shadow.ensure_table()
    tks = ["PB1.TW", "PB2.TW", "PB3.TW", "PB4.TW"]
    with store._conn() as conn:
        for t in tks:
            conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (t,))
    try:
        _ins("PB1.TW", "2031-03-05", "rejected", "gate_weekly_long")      # 10 天前：算
        _ins("PB1.TW", "2031-03-10", "rejected", "long_score")            # 5 天前：最近一筆
        _ins("PB2.TW", "2031-02-20", "rejected", "gate_weekly_long")      # 23 天前：超出回看
        _ins("PB3.TW", "2031-03-12", "rejected", "adx_low")               # 不是被停發類別
        rows = [{"bar_date": "2031-03-15", "ticker": t, "features": {}} for t in tks]
        shadow._add_prior_blocked(rows)
        got = {r["ticker"]: r["features"]["prior_blocked"] for r in rows}
        assert got == {"PB1.TW": "long_score", "PB2.TW": "none", "PB3.TW": "none", "PB4.TW": "none"}
    finally:
        with store._conn() as conn:
            for t in tks:
                conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (t,))


def test_prior_blocked_lookup_failure_leaves_feature_missing(monkeypatch):
    from state_store import store
    monkeypatch.setattr(store, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    rows = [{"bar_date": "2031-03-15", "ticker": "PB9.TW", "features": {}}]
    shadow._add_prior_blocked(rows)
    assert "prior_blocked" not in rows[0]["features"]            # 查不到＝缺漏，不寫 "none" 假裝沒有


def test_finalize_alt_exit_tracks_writes_once_and_is_idempotent():
    """資料庫層：只處理 Champion 已定案的候選；全部方案定案後不再重算；尚未出場者下次再算。"""
    from datetime import datetime
    from state_store import store
    shadow.ensure_table()
    tks = ("ALT1.TW", "ALT2.TW", "ALT3.TW")
    def _clean():
        with store._conn() as conn:
            for t in tks:
                conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (t,))
                conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (t,))
    _clean()
    try:
        with store._conn() as conn:
            for t, champ in (("ALT1.TW", "final"), ("ALT2.TW", "final"), ("ALT3.TW", "provisional")):
                conn.execute(
                    "INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,"
                    "status,result,r_multiple,champion_r,champion_status,features,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("2031-03-03", t, "9999", "測試", "candidate", "buy", 80, 100.0, 95.0, 107.5, 112.5, 120.0,
                     "closed", "sl", -1.0, -1.1 if champ == "final" else None, champ,
                     json.dumps({"sl_atr": 2.0}), "2031-03-03"))
            stop = [("2031-03-03", 100, 100, 100, 100), ("2031-03-04", 100, 101, 99, 100), ("2031-03-05", 100, 100, 94, 94.5)]
            flat = [("2031-03-03", 100, 100, 100, 100)] + [(f"2031-03-{d:02d}", 100, 101, 99, 100) for d in range(4, 12)]
            for t, seq in (("ALT1.TW", stop), ("ALT2.TW", flat), ("ALT3.TW", stop)):
                for d, o, h, l, c in seq:
                    conn.execute("INSERT OR REPLACE INTO ohlcv_bars (ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                                 "VALUES (?,?,?,?,?,?,?,?,?)", (t, "daily", d, o, h, l, c, 1000, d))
        today = datetime(2031, 3, 12)
        out = shadow.finalize_alt_exit_tracks(today=today)
        assert out["updated"] >= 2

        def alt(t):
            with store._conn() as conn:
                v = conn.execute("SELECT alt_exits FROM shadow_signals WHERE ticker=?", (t,)).fetchone()["alt_exits"]
            return json.loads(v) if v else None
        a1, a2 = alt("ALT1.TW"), alt("ALT2.TW")
        assert a1["hold60"]["status"] == "final" and a1["trail3"]["status"] == "final" and a1["sim"] == execution.ALT_EXIT_SIM_VERSION
        assert a2["hold60"] == {"status": "provisional", "r": None}          # 還沒出場：不存數字
        assert alt("ALT3.TW") is None                                        # Champion 尚未定案：不處理
        again = shadow.finalize_alt_exit_tracks(today=today)
        with store._conn() as conn:
            n1 = conn.execute("SELECT COUNT(*) AS n FROM shadow_signals WHERE ticker='ALT1.TW' AND alt_exits LIKE ?",
                              ('%"provisional"%',)).fetchone()["n"]
        assert n1 == 0 and again["updated"] == 0                             # 已定案者不再重選；未變者不重寫
    finally:
        _clean()
