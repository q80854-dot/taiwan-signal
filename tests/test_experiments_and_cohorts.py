"""預先登錄的 Challenger 影子實驗＋候選漏斗／成熟度／資料品質稽核（2026-11-12）。

審查報告 T.1：不得用提出假說的那段資料來驗證假說；新濾網只准做預先登錄的影子實驗。
S-P1-03：candidate ≠ sent ≠ delivered ≠ 成交。S-P1-08：只看已結案有截尾偏差。
"""
import experiments as ex
import shadow
from state_store import store

_TK = "EXPCOH.TW"


def _row(day, r, sentiment=60, chase="low", status="final", direction="buy"):
    return {"bar_date": day, "direction": direction, "status": "closed", "champion_r": r,
            "champion_status": status, "f": {"sentiment": sentiment, "chase_level": chase}}


def test_registry_is_frozen_and_documented():
    assert [e["id"] for e in ex.REGISTRY] == ["E01", "E02", "E09", "E10"] and ex.OOS_START > ex.FROZEN_AT
    for e in ex.REGISTRY:
        doc = open(e["doc"], encoding="utf-8").read()
        assert f"id: {e['id']}" in doc and e["filter"] in doc          # 程式與登錄文件的濾網定義一致
        assert f"min_oos_days: {ex.MIN_OOS_DAYS}" in doc and f"min_effect_r: {ex.MIN_EFFECT_R}" in doc
        assert f'oos_window: ">= {ex.OOS_START}"' in doc


def test_exploratory_window_can_never_be_a_verdict():
    """探索期就算差異再大、樣本再多，也只能是 exploratory_only。"""
    rows = []
    for i in range(60):
        day = f"2026-09-{(i % 28) + 1:02d}" if i < 28 else f"2026-10-{(i % 28) + 1:02d}"
        rows += [_row(day, 2.0, sentiment=70)] * 5 + [_row(day, -1.0, sentiment=30)] * 5
    out = ex.evaluate("E01", rows)
    assert out["exploratory"]["verdict"] == "exploratory_only"
    assert out["exploratory"]["diff_kept_minus_baseline"] > 1.0       # 差異很大也不算數
    assert out["oos"]["verdict"] == "insufficient" and out["oos"]["n_final"] == 0


def test_oos_insufficient_until_enough_days_and_trades():
    rows = [_row("2026-11-20", 1.0, sentiment=70), _row("2026-11-20", -1.0, sentiment=30)]
    o = ex.evaluate("E01", rows)["oos"]
    assert o["verdict"] == "insufficient" and o["n_days_baseline"] == 1
    assert "繼續累積" in o["verdict_note"]


def _oos_rows(kept_r, dropped_r, days=45, per=6):
    rows = []
    for i in range(days):
        day = f"2027-{1 + i // 28:02d}-{(i % 28) + 1:02d}"
        rows += [_row(day, kept_r + (0.1 if j % 2 else -0.1), chase="low") for j in range(per)]
        rows += [_row(day, dropped_r + (0.1 if j % 2 else -0.1), chase="high") for j in range(per)]
    return rows


def test_oos_supported_and_not_supported_follow_preregistered_rule():
    good = ex.evaluate("E02", _oos_rows(0.6, -0.6))["oos"]
    assert good["verdict"] == "supported" and good["diff_ci95"][0] > ex.MIN_EFFECT_R
    assert good["trade_retention"] == 0.5 and "不自動採用" in good["verdict_note"]
    flat = ex.evaluate("E02", _oos_rows(0.0, 0.0))["oos"]
    assert flat["verdict"] == "not_supported" and flat["diff_ci95"][1] < ex.MIN_EFFECT_R


def test_missing_feature_and_non_final_rows_are_not_guessed():
    rows = _oos_rows(0.5, -0.5, days=3)
    rows.append({"bar_date": "2027-01-01", "direction": "buy", "status": "closed", "champion_r": 9.9,
                 "champion_status": "final", "f": {}})                       # 缺特徵
    rows.append(_row("2027-01-01", 9.9, status="provisional"))               # 尚未定案
    rows.append(_row("2027-01-01", 9.9, direction="sell"))                   # 空方不混入做多實驗
    o = ex.evaluate("E02", rows)["oos"]
    assert o["n_missing_feature"] == 1 and o["n_baseline"] == 36             # 三筆 9.9 都沒進任何一組
    assert o["kept_avg_r"] == 0.5


def test_module_is_evaluation_only():
    src = open(ex.__file__, encoding="utf-8").read()
    assert "set_meta" not in src and "import config" not in src and "UPDATE " not in src
    assert ex.evaluate("E99")["reason"] == "unknown_experiment"


def _cleanup():
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker LIKE ?", (_TK + "%",))


def _ins(i, day, status, r=None, sent=0, delivered=None, champ_status=None, stop=95.0, mfe=None):
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,"
            "status,result,r_multiple,sent,delivered,champion_status,mfe_r,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day, f"{_TK}{i}", "9999", "測試", "candidate", "sell", 80, 100.0, stop, 95.0, 90.0, 85.0,
             status, "sl" if status == "closed" else None, r, sent, delivered, champ_status, mfe, day))


def test_cohort_funnel_and_maturity_separate_sent_from_candidates():
    _cleanup()
    try:
        before = shadow.cohort_report()["sell"]
        _ins(1, "2031-01-02", "closed", r=-1.0, sent=1, delivered=1, champ_status="final")
        _ins(2, "2031-01-02", "closed", r=-1.0)
        _ins(3, "2031-01-03", "closed", r=-1.0)                 # 這天還有一筆 pending → 未成熟
        _ins(4, "2031-01-03", "pending")
        c = shadow.cohort_report()["sell"]
        f, f0 = c["funnel"], before["funnel"]
        assert (f["candidate"] - f0["candidate"], f["sent"] - f0["sent"], f["delivered"] - f0["delivered"]) == (4, 1, 1)
        assert f["champion_final"] - f0["champion_final"] == 1 and f["filled"] is None    # 無成交回報：留空
        m, m0 = c["maturity"], before["maturity"]
        assert m["signal_days"] - m0["signal_days"] == 2 and m["mature_days"] - m0["mature_days"] == 1
        assert m["n_mature"] - m0["n_mature"] == 2 and m["pending"] - m0["pending"] == 1
    finally:
        _cleanup()


def test_data_quality_counts_expose_dirty_rows():
    _cleanup()
    try:
        b = shadow.data_quality_counts()
        _ins(5, "2031-02-01", "closed", r=None, stop=None, mfe=-8999999000.0)   # 歷史髒資料的樣貌
        _ins(6, "2031-02-01", "closed", r=-1.0, champ_status="provisional")
        a = shadow.data_quality_counts()
        assert a["closed_without_r"] - b["closed_without_r"] == 1
        assert a["closed_missing_stop"] - b["closed_missing_stop"] == 1
        assert a["absurd_mfe_mae"] - b["absurd_mfe_mae"] == 1
        assert a["champion_provisional"] - b["champion_provisional"] == 1
    finally:
        _cleanup()
