"""S-P0-04 逐列血緣：每筆影子列記下產生當時的策略版本；Champion 定案值記下模擬器版本；舊列不回填。"""
import execution
import shadow
import strategy_version as sv


def _row(ticker, day="2026-11-13", kind="candidate", direction="buy"):
    return {"bar_date": day, "ticker": ticker, "code": ticker.split(".")[0], "name": "測試", "kind": kind,
            "direction": direction, "score": 80, "entry": 100.0, "stop": 95.0, "tp1": 107.5, "tp2": 112.5,
            "tp3": 120.0, "size_cat": "中型股", "sector": "半導體業", "created_at": "2026-11-13T09:00:00+00:00",
            "features": {}}


def _get(ticker):
    from state_store import store
    with store._conn() as conn:
        return dict(conn.execute("SELECT * FROM shadow_signals WHERE ticker=?", (ticker,)).fetchone())


def _clear(*tickers):
    from state_store import store
    shadow.ensure_table()
    with store._conn() as conn:
        for t in tickers:
            conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (t,))


def test_new_rows_record_the_strategy_version_at_capture(monkeypatch):
    _clear("LIN1.TW")
    monkeypatch.setattr(shadow, "_add_sector_rel", lambda rows: None)
    shadow._insert([_row("LIN1.TW")])
    assert _get("LIN1.TW")["config_ver"] == sv.config_hash()[:12]
    _clear("LIN1.TW")


def test_version_lookup_failure_leaves_null_not_a_guess(monkeypatch):
    _clear("LIN2.TW")
    monkeypatch.setattr(shadow, "_add_sector_rel", lambda rows: None)
    monkeypatch.setattr(sv, "config_hash", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    shadow._insert([_row("LIN2.TW")])
    assert _get("LIN2.TW")["config_ver"] is None
    _clear("LIN2.TW")


def test_champion_final_records_simulator_version_only_when_final():
    f, v = shadow._champion_fields({"status": "final", "r": 0.5, "exit_date": "2026-11-20"})
    assert "champion_sim_ver=?" in f and v[-1] == execution.STAGED_SIM_VERSION
    f, v = shadow._champion_fields({"status": "provisional", "r": 0.2})
    assert not any("champion_sim_ver" in x for x in f)             # 暫估值不是定案，不標模擬器版本


def test_export_includes_lineage_columns(monkeypatch):
    _clear("LIN3.TW")
    monkeypatch.setattr(shadow, "_add_sector_rel", lambda rows: None)
    shadow._insert([_row("LIN3.TW")])
    text, _n = shadow.export_csv()
    head = text.lstrip("\ufeff").splitlines()[0].split(",")
    for col in ("config_ver", "champion_sim_ver", "feat_schema_ver"):
        assert head.count(col) == 1
    _clear("LIN3.TW")


def test_cohort_report_separates_strategy_versions(monkeypatch):
    from state_store import store
    _clear("LIN4.TW", "LIN5.TW", "LIN6.TW")
    monkeypatch.setattr(shadow, "_add_sector_rel", lambda rows: None)
    shadow._insert([_row("LIN4.TW", day="2031-01-02"), _row("LIN5.TW", day="2031-01-03"),
                    _row("LIN6.TW", day="2031-01-03")])
    with store._conn() as conn:
        conn.execute("UPDATE shadow_signals SET config_ver=NULL WHERE ticker='LIN4.TW'")       # 模擬舊列
        conn.execute("UPDATE shadow_signals SET config_ver='zzzztestver1', champion_r=0.4, champion_status='final' "
                     "WHERE ticker='LIN5.TW'")
        conn.execute("UPDATE shadow_signals SET config_ver='zzzztestver1', champion_r=9.9, "
                     "champion_status='provisional' WHERE ticker='LIN6.TW'")
    try:
        bv = shadow.cohort_report()["by_config_ver"]
        new = next(x for x in bv if x["config_ver"] == "zzzztestver1" and x["direction"] == "buy")
        assert new["candidates"] == 2 and new["champion_final"] == 1 and new["champion_avg_r"] == 0.4   # 暫估不進平均
        assert new["first_day"] == "2031-01-03"
        assert any(x["config_ver"] == "未記錄" for x in bv)          # 舊列如實標示，不回填
    finally:
        _clear("LIN4.TW", "LIN5.TW", "LIN6.TW")
