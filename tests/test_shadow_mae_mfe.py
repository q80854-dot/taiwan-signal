"""影子 strategy_signals 逐筆 MAE/MFE（資料契約缺口補齊 #4）。

§8.5 原列缺口：回撤等逐筆資料未逐欄記錄。補齊其中可由既有結算路徑真實取得者：
結算時以持有窗（首根之後到出場日）的 bars 計算有號極值 MAE/MFE（R 計、以首根開盤
fill 為基準、分母 |entry−stop|）——價格路徑極值、不含費稅，與 champion_r 費後口徑
不同、展示端分開標示。舊結算列 NULL＝誠實「無紀錄」，不回填、不猜值。
"""
import os

from strategies import store as sstore

_TK = "MAEMFE_TEST.TW"
_PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0,
         "tp1": 105.0, "tp2": 110.0, "tp3": 115.0, "score": 80, "reason": "x"}


def _cleanup():
    sstore.ensure_table()
    from state_store import store
    with store._conn() as conn:
        conn.execute("DELETE FROM strategy_signals WHERE ticker=?", (_TK,))
        conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (_TK,))


def _insert_bars(rows):
    from state_store import store
    with store._conn() as conn:
        for d, o, h, l, c in rows:
            conn.execute("INSERT OR REPLACE INTO ohlcv_bars (ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (_TK, "daily", d, o, h, l, c, 1000, d))


def _row():
    from state_store import store
    with store._conn() as conn:
        return dict(conn.execute("SELECT * FROM strategy_signals WHERE ticker=?", (_TK,)).fetchone())


def test_resolve_records_mae_mfe_buy():
    _cleanup()
    try:
        sstore.record_candidates("2026-10-01", _TK, {"code": "9998", "name": "測試"},
                                 [("trend_following", dict(_PLAN))])
        _insert_bars([("2026-10-02", 100, 106, 98, 105),     # fill=100、觸 tp1；最低 98
                      ("2026-10-03", 105, 111, 101, 110),    # 觸 tp2（低點不碰保本停損 100）
                      ("2026-10-04", 110, 116, 109, 115)])   # 觸 tp3 → 全出場；最高 116
        out = sstore.resolve_pending()
        assert out["closed"] == 1
        row = _row()
        # risk=|100-95|=5、fill=100：MFE=(116-100)/5=3.2、MAE=(98-100)/5=-0.4（有號）
        assert row["mfe_r"] == 3.2 and row["mae_r"] == -0.4
        ov = sstore.overview()
        tf = [s for s in ov["strategies"] if s["strategy_name"] == "trend_following"][0]
        assert tf["mae_mfe_n"] >= 1 and tf["avg_mae_r"] is not None     # 摘要帶逐筆統計
        assert any("MAE/MFE" in g for g in ov["gaps"])                   # 舊列無紀錄誠實列出
    finally:
        _cleanup()


def test_window_stops_at_exit_date():
    _cleanup()
    try:
        sstore.record_candidates("2026-10-01", _TK, {"code": "9998", "name": "測試"},
                                 [("trend_following", dict(_PLAN))])
        _insert_bars([("2026-10-02", 99, 101, 94, 95),       # fill=99、觸停損 → 10-02 出場
                      ("2026-10-03", 100, 150, 50, 100)])    # 出場後極端值，不得計入
        out = sstore.resolve_pending()
        assert out["closed"] == 1
        row = _row()
        assert row["exit_date"] == "2026-10-02"
        # 窗只到出場日：MFE=(101-99)/5=0.4、MAE=(94-99)/5=-1.0；150/50 不得汙染
        assert row["mfe_r"] == 0.4 and row["mae_r"] == -1.0
    finally:
        _cleanup()


def test_legacy_rows_stay_null_and_display_honest():
    _cleanup()
    try:
        from state_store import store
        with store._conn() as conn:
            conn.execute("INSERT INTO strategy_signals (bar_date,ticker,code,name,strategy_name,direction,"
                         "entry,stop,status,result,exit_price,exit_date,champion_r,created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("2026-09-01", _TK, "9998", "測試", "trend_following", "buy",
                          100.0, 95.0, "closed", "tp1", 105.0, "2026-09-05", 0.5, "2026-09-01"))
        assert _row()["mae_r"] is None and _row()["mfe_r"] is None       # 舊列 NULL 不回填
        html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "templates", "dashboard.html"), encoding="utf-8").read()
        assert "平均MAE／MFE（R，價格路徑）" in html and "無紀錄" in html   # 前端欄位＋無紀錄誠實字樣
        assert "不含費稅" in html                                         # 口徑警語（與費後淨R分開）
    finally:
        _cleanup()
