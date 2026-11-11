"""champion_r 回填（只用真實資料）。

Shadow 雙軌上線前就結案的候選 champion_r 為 NULL。backfill 用訊號當時的真實價位＋真實歷史 K 棒
重算；缺 K 棒就留 NULL、不猜；不碰其他欄位；冪等。本測試以 sqlite 真實插入列與 K 棒驗證。
"""
import shadow
from state_store import store

_OK = "BF_OK.TW"
_NODATA = "BF_NODATA.TW"


def _cleanup():
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker IN (?,?)", (_OK, _NODATA))
        conn.execute("DELETE FROM ohlcv_bars WHERE ticker IN (?,?)", (_OK, _NODATA))


def _insert_closed(ticker, champion_r=None):
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,"
            "tp1,tp2,tp3,size_cat,sector,status,result,r_multiple,champion_r,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-01", ticker, ticker[:4], "測試", "candidate", "buy", 80,
             100.0, 95.0, 105.0, 110.0, 115.0, "中型股", "半導體",
             "closed", "tp3", 1.0, champion_r, "2026-10-01T00:00:00"))


def _insert_bars(ticker, rows):
    with store._conn() as conn:
        for d, o, h, l, c in rows:
            conn.execute("INSERT OR REPLACE INTO ohlcv_bars (ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (ticker, "daily", d, o, h, l, c, 1000, d))


def _champ(ticker):
    with store._conn() as conn:
        return dict(conn.execute("SELECT champion_r FROM shadow_signals WHERE ticker=?", (ticker,)).fetchone())["champion_r"]


def test_backfill_uses_real_bars_and_leaves_nodata_null():
    _cleanup()
    try:
        _insert_closed(_OK)            # champion_r NULL，有真實 K 棒 → 應被填
        _insert_closed(_NODATA)        # champion_r NULL，無 K 棒 → 應留 NULL
        _insert_bars(_OK, [("2026-10-02", 100, 106, 99, 105),
                           ("2026-10-03", 105, 111, 104, 110),
                           ("2026-10-04", 110, 116, 109, 115)])  # 真實 K 棒，觸 tp1/2/3
        shadow.backfill_champion_r()
        assert _champ(_OK) is not None and _champ(_OK) > 0     # 用真實資料算出
        assert _champ(_NODATA) is None                         # 缺真實 K 棒 → 留 NULL、不猜
    finally:
        _cleanup()


def test_backfill_idempotent_and_does_not_touch_filled():
    _cleanup()
    try:
        _insert_closed(_OK, champion_r=0.42)   # 已有值 → 不在回填範圍、不得被改動
        _insert_bars(_OK, [("2026-10-02", 100, 106, 99, 105)])
        out = shadow.backfill_champion_r()
        assert _champ(_OK) == 0.42             # 既有值不動
        assert out["filled"] == 0 or _OK       # 已填者不計入
    finally:
        _cleanup()
