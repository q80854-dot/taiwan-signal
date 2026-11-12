"""P0 資料可信度：MAE/MFE/R 爆量防護與既有髒列清洗（2026-11-06）。
確保 entry≈stop 退化列不產生 ±1e9 的 R，既有爆量列被清洗出統計，正常列不受影響。"""
import shadow


# ── _risk_ok ──
def test_risk_ok_normal():
    assert shadow._risk_ok(600.0, 570.0) is True          # 停損 5%，正常


def test_risk_ok_rejects_degenerate():
    assert shadow._risk_ok(600.0, 600.0) is False          # entry==stop
    assert shadow._risk_ok(600.0, 599.9999) is False       # 浮點微差（risk≈1e-4 << 600*0.05%)
    assert shadow._risk_ok(600.0, 0) is False
    assert shadow._risk_ok(0, 0) is False
    assert shadow._risk_ok(None, 1) is False
    assert shadow._risk_ok("x", "y") is False


# ── _simulate 不對退化風險產 R ──
def _bars(n=6, base=100.0):
    out = []
    for i in range(n):
        p = base + i
        out.append({"bar_date": f"2026-10-{i+2:02d}", "open": p, "high": p + 1, "low": p - 1, "close": p})
    return out


def test_simulate_rejects_degenerate_risk():
    row = {"entry": 100.0, "stop": 100.0, "direction": "buy", "bar_date": "2026-10-01",
           "tp1": 105, "tp2": 110, "tp3": 115}
    assert shadow._simulate(row, _bars(), 15) is None      # entry==stop → 不產 R


def test_simulate_normal_risk_produces_bounded_r():
    row = {"entry": 100.0, "stop": 95.0, "direction": "buy", "bar_date": "2026-10-01",
           "tp1": 105, "tp2": 110, "tp3": 115}
    sim = shadow._simulate(row, _bars(), 15)
    assert sim is not None
    for k in ("mfe", "mae", "r1"):
        assert sim[k] is None or abs(sim[k]) <= shadow._ABSURD_R   # 絕不爆量


# ── cleanup_absurd_metrics 清洗既有髒列 ──
def _insert(store, ticker, r_multiple=None, mae_r=None, status="closed", result="tp1"):
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (ticker,))  # 共用 temp DB，先清同 ticker 保證隔離
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,kind,direction,score,entry,stop,"
            "status,result,r_multiple,mae_r,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-01", ticker, "candidate", "buy", 80, 600, 570,
             status, result, r_multiple, mae_r, "2026-10-01T00:00:00"))


def _get(store, ticker):
    with store._conn() as conn:
        return dict(conn.execute("SELECT * FROM shadow_signals WHERE ticker=?", (ticker,)).fetchone())


def test_cleanup_reclassifies_absurd_and_keeps_good(monkeypatch):
    from state_store import store
    # 重置旗標，確保測試能實際跑清洗
    shadow._cleanup_done = False
    try:
        store.set_meta(shadow._CLEANUP_META, None)
    except Exception:
        pass
    _insert(store, "BAD1.TW", r_multiple=8.9e9, mae_r=-9e8)    # 爆量
    _insert(store, "BAD2.TW", r_multiple=0.5, mae_r=-5e5)      # mae_r 爆量
    _insert(store, "GOOD1.TW", r_multiple=1.8, mae_r=-0.9)     # 正常
    out = shadow.cleanup_absurd_metrics(force=True)
    assert out["ran"] is True and out["cleaned"] >= 2
    b1, b2, g1 = _get(store, "BAD1.TW"), _get(store, "BAD2.TW"), _get(store, "GOOD1.TW")
    assert b1["status"] == "unresolved" and b1["result"] == "bad_risk" and b1["r_multiple"] is None
    assert b2["status"] == "unresolved" and b2["r_multiple"] is None and b2["mae_r"] is None
    assert g1["status"] == "closed" and g1["r_multiple"] == 1.8 and g1["mae_r"] == -0.9


def test_cleanup_runs_every_time_and_catches_rows_dirtied_later(monkeypatch):
    """2026-11-12：清洗不再只跑一次。上線後的稽核計數顯示一次性清洗之後仍有髒列再生，
    必須每次結算都檢查；重跑本身冪等（沒有新髒列就清 0 筆、不動正常列）。"""
    from state_store import store
    shadow._cleanup_done = False
    store.set_meta(shadow._CLEANUP_META, None)
    _insert(store, "IDEMP1.TW", r_multiple=1.0)
    first = shadow.cleanup_absurd_metrics()
    second = shadow.cleanup_absurd_metrics()
    assert first["ran"] is True and second["ran"] is True and second["cleaned"] == 0   # 冪等
    assert _get(store, "IDEMP1.TW")["r_multiple"] == 1.0
    _insert(store, "LATEBAD.TW", r_multiple=0.4, mae_r=-8.9e9)       # 第一次清洗「之後」才出現的髒列
    third = shadow.cleanup_absurd_metrics()
    assert third["cleaned"] >= 1
    late = _get(store, "LATEBAD.TW")
    assert late["status"] == "unresolved" and late["result"] == "bad_risk" and late["mae_r"] is None


def test_unresolved_rows_are_not_reselected_and_redirtied(monkeypatch):
    """已標 unresolved 的列不得再被撈回來重新模擬（原本 r10 IS NULL 的條件會撈到，髒值因此再生）。"""
    seen = []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, p=()):
            seen.append(" ".join(q.split()))
            return self
        def fetchall(self): return []
        def fetchone(self): return None
    from state_store import store
    monkeypatch.setattr(store, "_conn", lambda: _Conn())
    monkeypatch.setattr(shadow, "ensure_table", lambda: None)
    monkeypatch.setattr(shadow, "cleanup_absurd_metrics", lambda *a, **k: None)
    shadow.resolve_pending()
    sel = [q for q in seen if q.startswith("SELECT * FROM shadow_signals WHERE status='pending'")]
    assert sel and "(status='closed' AND r10 IS NULL AND bar_date>=?)" in sel[0]
