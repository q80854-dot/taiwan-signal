"""P0：sent→delivered 真對帳（2026-11-06）。
sent=1 只代表『排入推播』；delivered=1 才代表 Telegram 真的收下。push_signal 回傳 any_ok，
scanner 據此寫 shadow.delivered，使『sent=1 且 delivered=0』＝排了卻沒送達，可被查出。"""
import shadow
import telegram_bot as tb


# ── push_signal any_ok ──
def _patch_push(monkeypatch, send_ok=True, broadcast_n=1, admin=("A1",)):
    monkeypatch.setattr(tb, "TELEGRAM_FREE_CHANNEL", "", raising=False)
    monkeypatch.setattr(tb, "TELEGRAM_PAID_CHANNEL", "", raising=False)
    monkeypatch.setattr(tb, "broadcast", lambda text, tier="free": broadcast_n)
    monkeypatch.setattr(tb, "_load_subscribers", lambda: {"admin": list(admin), "free": [], "paid": []})
    monkeypatch.setattr(tb, "send_message", lambda cid, text, *a, **k: send_ok)
    monkeypatch.setattr(tb, "format_signal_paid", lambda sig: "msg")
    monkeypatch.setattr(tb.time, "sleep", lambda *a, **k: None)


def _sig():
    return {"ticker": "2330.TW", "name": "台積電", "direction": "buy", "score": 85}


def test_push_signal_any_ok_true_when_admin_ok(monkeypatch):
    _patch_push(monkeypatch, send_ok=True, broadcast_n=0, admin=("A1",))
    out = tb.push_signal(_sig())
    assert out["any_ok"] is True and out["admin_ok"] == 1


def test_push_signal_any_ok_true_when_broadcast_ok(monkeypatch):
    _patch_push(monkeypatch, send_ok=False, broadcast_n=3, admin=())
    out = tb.push_signal(_sig())
    assert out["any_ok"] is True and out["free_ok"] is True


def test_push_signal_any_ok_false_when_all_fail(monkeypatch):
    _patch_push(monkeypatch, send_ok=False, broadcast_n=0, admin=("A1",))
    out = tb.push_signal(_sig())
    assert out["any_ok"] is False and out["admin_ok"] == 0


# ── mark_delivered 只在真送達時標記 ──
def _insert_sent(store, ticker):
    shadow.ensure_table()
    with store._conn() as conn:
        conn.execute("DELETE FROM shadow_signals WHERE ticker=?", (ticker,))
        conn.execute(
            "INSERT INTO shadow_signals (bar_date,ticker,kind,direction,score,entry,stop,"
            "status,sent,delivered,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-11-06", ticker, "candidate", "buy", 85, 600, 570, "pending", 1, 0, "2026-11-06T00:00:00"))


def _row(store, ticker):
    with store._conn() as conn:
        return dict(conn.execute("SELECT * FROM shadow_signals WHERE ticker=?", (ticker,)).fetchone())


def test_mark_delivered_sets_only_confirmed():
    from state_store import store
    _insert_sent(store, "DLV_OK.TW")
    _insert_sent(store, "DLV_FAIL.TW")
    shadow.mark_delivered({"DLV_OK.TW": True, "DLV_FAIL.TW": False})
    ok, fail = _row(store, "DLV_OK.TW"), _row(store, "DLV_FAIL.TW")
    # 兩者 sent 都=1（都排入推播），但只有真送達的 delivered=1
    assert ok["sent"] == 1 and ok["delivered"] == 1 and ok["delivered_at"]
    assert fail["sent"] == 1 and fail["delivered"] == 0 and not fail["delivered_at"]


def test_sent_not_equal_delivered_is_queryable():
    """核心：能查出『排了推播但 Telegram 沒收下』的訊號。"""
    from state_store import store
    _insert_sent(store, "QRY1.TW")      # sent=1, delivered=0（未送達）
    _insert_sent(store, "QRY2.TW")
    shadow.mark_delivered({"QRY2.TW": True})
    with store._conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM shadow_signals WHERE sent=1 AND (delivered IS NULL OR delivered=0) "
            "AND ticker IN ('QRY1.TW','QRY2.TW')").fetchone()
    assert dict(n)["n"] == 1             # 只有 QRY1 未送達
