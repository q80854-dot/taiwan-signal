"""S-P0-01／S-P1-02：Research 與 Champion 兩軌各自的生命週期（2026-11-12）。

問題：Research 軌（單次出場）第一次觸價就結案；Champion 軌是分批出場，TP1 觸及後還有 2/3 部位
在場。原本在 Research 結案當下就把 champion_r 寫死，而分批模擬對「還沒出完、也還沒到期」的部位
是用最後一根已知收盤暫估——之後不再更新，暫估值被當成定案值。上傳的歷史 CSV 中，1,995 筆已結案
候選有 338 筆（約 17%）屬於這種「到期前因停利結案」的情形。

修正後：champion_r 只存定案值；暫估值另存 champion_r_prov（provisional），最後一腿真的出場或
到期才定案；資料在到期前停更者標 no_fresh_price，不以舊收盤價代替。舊值保留於 champion_r_legacy。
以 sqlite 真實插入列與 K 棒驗證（報告 S.5 的最小重現情境）。
"""
from datetime import datetime, timedelta

import execution
import shadow
from state_store import store
from strategies import store as sstore

_TK = "CHLIFE.TW"
_NOW = datetime.now(shadow.TPE).replace(tzinfo=None)


def _d(days_ago):
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _cleanup():
    shadow.ensure_table(); sstore.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (_TK,))
        conn.execute("DELETE FROM strategy_signals WHERE ticker=?", (_TK,))
        conn.execute("DELETE FROM ohlcv_bars WHERE ticker=?", (_TK,))


def _insert(bar_date, status="pending", result=None, r_multiple=None, champion_r=None):
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,"
            "tp1,tp2,tp3,size_cat,sector,status,result,r_multiple,champion_r,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bar_date, _TK, "9999", "測試", "candidate", "buy", 80, 100.0, 95.0, 105.0, 110.0, 115.0,
             "中型股", "半導體", status, result, r_multiple, champion_r, bar_date + "T00:00:00"))


def _bars(rows):
    with store._conn() as conn:
        for d, o, h, l, c in rows:
            conn.execute("INSERT OR REPLACE INTO ohlcv_bars (ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (_TK, "daily", d, o, h, l, c, 1000, d))


def _row():
    with store._conn() as conn:
        return dict(conn.execute("SELECT * FROM shadow_signals WHERE ticker=?", (_TK,)).fetchone())


def test_shadow_champion_stays_open_after_research_tp1():
    """D1 觸 TP1：Research 結案；Champion 只出第一腿，其餘 2/3 仍在場 → 不得寫入定案值。"""
    _cleanup()
    try:
        _insert(_d(5))
        _bars([(_d(5), 99, 101, 98, 100),            # 訊號日
               (_d(4), 100, 106, 99.5, 104)])        # D1：觸 TP1（105）
        shadow.resolve_pending()
        r = _row()
        assert r["status"] == "closed" and r["result"] == "tp1" and r["r_multiple"] is not None
        assert r["champion_status"] == "provisional"
        assert r["champion_r"] is None               # 定案欄不得有值
        assert r["champion_r_prov"] is not None      # 暫估另存，僅供參考
        # D2 盤整：仍未定案，重跑冪等
        _bars([(_d(3), 104, 104.5, 103, 104)])
        out = shadow.finalize_champion_tracks()
        r = _row()
        assert r["champion_status"] == "provisional" and r["champion_r"] is None
        assert out["still_provisional"] == 1
    finally:
        _cleanup()


def test_shadow_champion_final_only_after_last_leg():
    """D3 觸 TP2、D4 觸 TP3：最後一腿出場，此時才產生定案值（三段各 1/3 ≈ 2R 扣成本）。"""
    _cleanup()
    try:
        _insert(_d(6))
        _bars([(_d(6), 99, 101, 98, 100),
               (_d(5), 100, 106, 99.5, 104)])        # D1：TP1
        shadow.resolve_pending()
        prov = _row()["champion_r_prov"]
        _bars([(_d(4), 104, 104.5, 103, 104),        # D2：盤整
               (_d(3), 104, 111, 103.5, 110),        # D3：TP2
               (_d(2), 110, 116, 109, 115)])         # D4：TP3
        out = shadow.finalize_champion_tracks()
        r = _row()
        assert r["champion_status"] == "final" and out["finalized"] == 1
        assert 1.8 < r["champion_r"] < 2.0           # (1R+2R+3R)/3＝2R，扣費稅滑價略低
        assert r["champion_r_prov"] is None
        assert r["champion_r"] > prov                # 暫估（剩餘以 D1 收盤 104 估）低於真實結果
        # 冪等：再跑一次不變、也不再被掃到
        again = shadow.finalize_champion_tracks()
        assert again["scanned"] == 0 and _row()["champion_r"] == r["champion_r"]
    finally:
        _cleanup()


def test_shadow_champion_tp1_then_breakeven_stop_is_final():
    """D1 TP1、D2 跌破移動停損（保本）→ 全數出場即定案（1/3R 再扣全部位的來回成本）。"""
    _cleanup()
    try:
        _insert(_d(5))
        _bars([(_d(5), 99, 101, 98, 100),
               (_d(4), 100, 106, 99.5, 104),         # TP1 → 停損移至成本 100
               (_d(3), 103, 103.5, 99, 99.5)])       # 跌破 100 → 剩餘 2/3 保本出場
        shadow.resolve_pending()
        r = _row()
        assert r["champion_status"] == "final"
        assert 0.1 < r["champion_r"] < 0.34            # 1/3×1R，其餘保本出場仍付來回成本
    finally:
        _cleanup()


def test_shadow_champion_no_fresh_price_is_not_guessed():
    """TP1 後資料停更、過了到期日＋寬限仍無新棒：不得用舊收盤價當到期價。"""
    _cleanup()
    try:
        _insert(_d(5))
        _bars([(_d(5), 99, 101, 98, 100), (_d(4), 100, 106, 99.5, 104)])
        shadow.resolve_pending()
        assert _row()["champion_status"] == "provisional"
        out = shadow.finalize_champion_tracks(today=_NOW + timedelta(days=40))
        r = _row()
        assert r["champion_status"] == "no_fresh_price" and out["no_fresh_price"] == 1
        assert r["champion_r"] is None and r["champion_r_prov"] is None
        # 之後資料補上 → 仍可重新定案（不是永久判死）
        _bars([(_d(3), 104, 111, 103.5, 110), (_d(2), 110, 116, 109, 115)])
        shadow.finalize_champion_tracks(today=_NOW + timedelta(days=40))
        assert _row()["champion_status"] == "final"
    finally:
        _cleanup()


def test_legacy_champion_r_is_reaudited_and_preserved():
    """舊列（結案當下寫死的值）以真實 K 棒重算；舊值保留於 champion_r_legacy，並回報差異。"""
    _cleanup()
    try:
        _insert(_d(6), status="closed", result="tp1", r_multiple=0.9, champion_r=0.85)   # 舊的暫估值
        _bars([(_d(6), 99, 101, 98, 100), (_d(5), 100, 106, 99.5, 104),
               (_d(4), 104, 111, 103.5, 110), (_d(3), 110, 116, 109, 115)])
        out = shadow.finalize_champion_tracks()
        r = _row()
        assert r["champion_r_legacy"] == 0.85                      # 舊值不覆寫、不刪
        assert r["champion_status"] == "final" and r["champion_r"] > 1.8
        assert out["legacy_checked"] == 1 and out["legacy_changed"] == 1
        assert out["legacy_max_abs_diff"] > 0.9
    finally:
        _cleanup()


def test_legacy_champion_r_without_bars_is_quarantined():
    """舊值已無 K 棒可重驗：移出統計（champion_r=NULL），值仍留在 champion_r_legacy。"""
    _cleanup()
    try:
        _insert(_d(6), status="closed", result="tp1", r_multiple=0.9, champion_r=0.85)
        out = shadow.finalize_champion_tracks()
        r = _row()
        assert r["champion_status"] == "legacy_unverified" and r["champion_r"] is None
        assert r["champion_r_legacy"] == 0.85 and out["legacy_unverified"] == 1
    finally:
        _cleanup()


def test_research_expiry_needs_bar_at_or_after_expiry_date():
    """S-P1-02（Research 軌）：已過到期日但 K 棒停在到期日前 → 不以舊收盤結算；
    寬限內留 pending，超過寬限標 unresolved/no_fresh_price。"""
    _cleanup()
    try:
        _insert(_d(17))                                            # 已過 15 天到期、未過寬限
        _bars([(_d(17), 99, 101, 98, 100), (_d(16), 100, 102, 98.5, 101)])   # 之後停更、未觸價
        res = shadow.resolve_pending()
        r = _row()
        assert r["status"] == "pending" and r["r_multiple"] is None and res["closed"] == 0
    finally:
        _cleanup()
    try:
        _insert(_d(30))                                            # 超過到期＋寬限
        _bars([(_d(30), 99, 101, 98, 100), (_d(29), 100, 102, 98.5, 101)])
        res = shadow.resolve_pending()
        r = _row()
        assert r["status"] == "unresolved" and r["result"] == "no_fresh_price"
        assert r["r_multiple"] is None and res["no_fresh_price"] == 1
    finally:
        _cleanup()


def test_research_expiry_settles_normally_with_fresh_bar():
    """有到期日當天或之後的 K 棒 → 照常以該棒收盤結算 expired（既有行為不變）。"""
    _cleanup()
    try:
        _insert(_d(20))
        _bars([(_d(20), 99, 101, 98, 100), (_d(19), 100, 102, 98.5, 101),
               (_d(4), 101, 102, 100, 101.5)])                     # 天齡 16 ≥ 15
        shadow.resolve_pending()
        r = _row()
        assert r["status"] == "closed" and r["result"] == "expired" and r["exit_date"] == _d(4)
        assert r["champion_status"] == "final"
    finally:
        _cleanup()


def test_strategy_store_does_not_settle_on_stale_price():
    """S-P1-02（策略假想訊號）：過期但 K 棒停更 → 寬限內留 pending；超過寬限 no_fresh_price。"""
    plan = {"direction": "long", "entry": 100.0, "stop": 95.0, "tp1": 105.0, "tp2": 110.0,
            "tp3": 115.0, "score": 80, "reason": "x"}
    for age, expect in ((17, ("pending", None)), (30, ("unresolved", "no_fresh_price"))):
        _cleanup()
        try:
            sstore.record_candidates(_d(age), _TK, {"code": "9999", "name": "測試"}, [("trend_following", dict(plan))])
            _bars([(_d(age), 99, 101, 98, 100), (_d(age - 1), 100, 106, 99.5, 104)])   # TP1 後停更
            sstore.resolve_pending()
            with store._conn() as conn:
                r = dict(conn.execute("SELECT status,result,champion_r FROM strategy_signals WHERE ticker=?", (_TK,)).fetchone())
            assert (r["status"], r["result"]) == expect and r["champion_r"] is None
        finally:
            _cleanup()


def test_staged_open_at_end_helper():
    pnl = lambda e, x, d, s, days_held=0: (x - e) * s
    kw = dict(entry=100, sl=95, tp1=105, tp2=110, tp3=115, direction="buy", shares=300,
              gen_date="2026-10-01", pnl_fn=pnl, expire_days=15)
    open_sim = execution.simulate_staged_exit([{"bar_date": "2026-10-02", "open": 100, "high": 106, "low": 99.5, "close": 104}], **kw)
    assert execution.staged_open_at_end(open_sim, "2026-10-01", 15) is True          # TP1 後仍在場、未到期
    exp_sim = execution.simulate_staged_exit([{"bar_date": "2026-10-02", "open": 100, "high": 106, "low": 99.5, "close": 104},
                                              {"bar_date": "2026-10-16", "open": 104, "high": 104.5, "low": 103, "close": 104}], **kw)
    assert execution.staged_open_at_end(exp_sim, "2026-10-01", 15) is False          # 真正到期結算
    sl_sim = execution.simulate_staged_exit([{"bar_date": "2026-10-02", "open": 100, "high": 101, "low": 94, "close": 95}], **kw)
    assert execution.staged_open_at_end(sl_sim, "2026-10-01", 15) is False           # 停損全出
