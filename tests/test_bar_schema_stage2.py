"""逐棒資料契約階段 2（LIVE10 缺口，2026-11-12 使用者核准「按上面方案進行」）。

讀取端使用血緣欄位：get_cached_ohlcv_bars 帶出四欄；fetch 路徑回傳
last_bar_final/last_bar_source/last_bar_source_time/last_bar_session；
前端「盤中（暫定）／收盤」優先吃逐棒 is_final 真實欄位（舊棒 NULL 退回 info.live 推斷
＝備援、不假裝）；掃描每筆訊號附末棒定案狀態、audit meta 記 bar_final_summary。
"""
import os

import data_fetcher as df
from state_store import store

_TK = "BARSCHEMA2_TEST.TW"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cleanup():
    with store._conn() as conn:
        conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (_TK,))


def test_fetch_returns_last_bar_lineage(monkeypatch):
    _cleanup()
    try:
        bars = [{"date": f"2026-10-{i:02d}", "open": 10, "high": 11, "low": 9,
                 "close": 10.5, "volume": 100} for i in range(1, 25)]
        bars[-1].update({"is_final": 0, "session": "regular",
                         "source": "yfinance", "source_time": "2026-10-24T10:00:00"})
        store.upsert_ohlcv_bars(_TK, "daily", bars)
        monkeypatch.setattr(df, "YFINANCE_OK", False)          # 走純快取路徑
        out = df._fetch_ohlcv_incremental(_TK, "daily")
        assert out is not None
        assert out["last_bar_final"] == 0                       # 末棒暫定
        assert out["last_bar_source"] == "yfinance"
        assert out["last_bar_source_time"] == "2026-10-24T10:00:00"
        # 讀取端帶出血緣欄（舊棒 NULL 誠實保留）
        rows = store.get_cached_ohlcv_bars(_TK, "daily", limit=25)
        assert rows[0]["is_final"] is None and rows[-1]["is_final"] == 0
    finally:
        _cleanup()


def test_dashboard_prefers_real_is_final_with_honest_fallback():
    s = open(os.path.join(_ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "last_bar_final===1" in s and "last_bar_final===0" in s   # 吃真實逐棒欄位
    assert "收盤（K棒已定案）" in s                                   # 定案明標
    assert "盤中（暫定，未收盤" in s                                  # 暫定字樣保留
    assert "info.live?_prov" in s                                     # 舊棒無紀錄 → 誠實退回推斷備援


def test_scanner_attaches_bar_lineage_and_summary():
    s = open(os.path.join(_ROOT, "scanner.py"), encoding="utf-8").read()
    assert '_s["bar_final"]' in s and '_s["bar_date_used"]' in s      # 每筆訊號附末棒狀態
    assert '"bar_final_summary": _bar_sum' in s                        # audit meta 統計
    assert '"unknown"' in s[s.index("_bar_sum"):s.index("_bar_sum") + 800]  # 舊棒無紀錄誠實計 unknown
