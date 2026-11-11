"""美編 P2-E（brief §8.5 影子追蹤展示）：影子策略並排、假設/結論狀態、不可比較、缺口誠實。

後端 strategies.store.overview()：
- 依註冊表列出策略（即使 0 樣本也顯示）、附研究假設（編號/指標/驗收/立論）。
- 未結/已結算/勝負/日期範圍、Champion 共同量尺平均淨 R。
- 結論狀態：已結算 < MIN_EVAL_SAMPLES 一律「資料不足」，不下結論。
- 缺口（回撤/換手/風險事件）明列、不猜值。

前端：策略學習頁「影子策略」分組含不可比較說明、假設與結論狀態、不授權切換正式策略。
"""
import os

import strategies.store as sstore

_TK = "P2E_TEST.TW"
_PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0,
         "tp1": 105.0, "tp2": 110.0, "tp3": 115.0, "score": 80, "reason": "x"}


def _cleanup():
    sstore.ensure_table()
    from state_store import store
    with store._conn() as conn:
        conn.execute("DELETE FROM strategy_signals WHERE ticker=?", (_TK,))


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_overview_lists_registered_with_hypothesis_even_at_zero_samples():
    _cleanup()
    try:
        ov = sstore.overview()
        tf = [s for s in ov["strategies"] if s["strategy_name"] == "trend_following"]
        assert tf, "註冊策略即使 0 樣本也應列出"
        s = tf[0]
        assert s["label"] == "趨勢追蹤" and s["direction"] == "long"
        assert s["hypothesis"] and s["hypothesis"]["id"] == "H-TF-01"
        for k in ("total", "open", "closed", "wins", "losses", "avg_champion_r", "first_date", "last_date"):
            assert k in s
        assert s["status"]["state"] == "insufficient"           # 未達門檻＝資料不足、不下結論
        assert ov["min_eval_samples"] == sstore.MIN_EVAL_SAMPLES
        assert ov["gaps"] and "回撤" in ov["gaps"][0]            # 缺口明列
        assert "不授權切換正式策略" in ov["note"] and "共同量尺" in ov["note"]
    finally:
        _cleanup()


def test_overview_counts_wins_losses_and_open(monkeypatch):
    _cleanup()
    try:
        from state_store import store
        # 直接插入兩筆已結算（一勝一負）＋一筆未結，驗證聚合帳目
        with store._conn() as conn:
            base = ("2026-10-01", _TK, "9999", "測試", "trend_following", "long",
                    100.0, 95.0, 105.0, 110.0, 115.0, 80.0, "x")
            conn.execute("INSERT INTO strategy_signals (bar_date,ticker,code,name,strategy_name,direction,"
                         "entry,stop,tp1,tp2,tp3,score,reason,status,result,champion_r,exit_date) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'closed','tp1',1.5,'2026-10-05')", base)
            conn.execute("INSERT INTO strategy_signals (bar_date,ticker,code,name,strategy_name,direction,"
                         "entry,stop,tp1,tp2,tp3,score,reason,status,result,champion_r,exit_date) "
                         "VALUES ('2026-10-02',?,?,?,?,?,?,?,?,?,?,?,?,'closed','sl',-1.0,'2026-10-06')",
                         (_TK, "9999", "測試", "trend_following", "long", 100.0, 95.0, 105.0, 110.0, 115.0, 80.0, "x"))
            conn.execute("INSERT INTO strategy_signals (bar_date,ticker,code,name,strategy_name,direction,"
                         "entry,stop,tp1,tp2,tp3,score,status) "
                         "VALUES ('2026-10-03',?,?,?,?,?,?,?,?,?,?,?,'pending')",
                         (_TK, "9999", "測試", "trend_following", "long", 100.0, 95.0, 105.0, 110.0, 115.0, 80.0))
        ov = sstore.overview()
        tf = [s for s in ov["strategies"] if s["strategy_name"] == "trend_following"][0]
        assert tf["closed"] >= 2 and tf["wins"] >= 1 and tf["losses"] >= 1
        assert tf["open"] >= 1
        assert tf["first_date"] == "2026-10-01"
    finally:
        _cleanup()


def test_frontend_shadow_group_present():
    s = _dash()
    assert 'lrnNav(\'shadow\',this)' in s and 'data-g="shadow"' in s
    assert 'id="lrn-shadow"' in s and "function loadShadowStrategies" in s
    assert "/api/strategy/shadow" in s
    assert "不可直接比較" in s and "不授權切換正式策略" in s        # §8.5 鐵律
    assert "研究假設與結論狀態" in s                                # 假設區塊
    assert "資料缺口" in s                                          # 缺口誠實顯示


def test_backend_api_registered():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    a = open(p, encoding="utf-8").read()
    assert '@app.route("/api/strategy/shadow")' in a
    assert "owner_required" in a[a.index('/api/strategy/shadow'):a.index('/api/strategy/shadow') + 300]
    assert "不授權切換正式策略" in a and "compare_caveat" in a
