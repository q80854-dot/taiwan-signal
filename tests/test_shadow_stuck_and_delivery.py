"""後台兩則長期告警的根因修正（2026-11-12）。

1) 「有追蹤中的紀錄從 X 起仍未結算，超過有效期，可能結算流程卡住」
   根因：標的有「舊」K 棒、但訊號日之後一根都沒有（停牌／下市／資料源停更）時，
   _simulate 回 result=None，而逾期保護只擋「完全沒有 K 棒」→ 永遠留在 pending。
   修正：逾期＋寬限後標 unresolved/no_new_bars 離開佇列，誠實列入原因拆分，不猜結果。

2) 「推播送達對帳：排入 N 筆、確認送達 M 筆（N-M 筆未確認）」
   根因：delivered 欄 2026-11-06 才新增，更早的推播結構上無對帳欄位（NULL）、永遠無法確認；
   原本把它們一起算進分母 → 已確認率長期偏低並持續誤報。
   修正：分母正名為「可對帳母體」（已確認＋可對帳未確認），無對帳欄位的舊列單獨揭露、
   不納入比率、不回填；mark_sent 起把 delivered 初始化為 0，往後每筆都可對帳。
"""
import shadow


def _stub_store(rows, captured):
    class _Store:
        def _conn(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executescript(self, q): return self

        def execute(self, q, p=()):
            s = q.strip()
            if s.startswith("SELECT"):
                self._rows = [dict(r) for r in rows]
                return self
            if s.startswith("UPDATE"):
                captured.append((q, p))
            return self

        def fetchall(self): return getattr(self, "_rows", [])
        def fetchone(self): return None
    return _Store()


def _run_resolve(monkeypatch, rows, bars):
    captured = []
    import state_store
    monkeypatch.setattr(state_store, "store", _stub_store(rows, captured))
    monkeypatch.setattr(shadow, "ensure_table", lambda: None)
    monkeypatch.setattr(shadow, "cleanup_absurd_metrics", lambda: None)
    monkeypatch.setattr(shadow, "_load_bars", lambda tickers, since: bars)
    monkeypatch.setattr(shadow, "_rescale", lambda row, bars: (dict(row), 1.0))
    res = shadow.resolve_pending()
    return res, captured


def _row(bar_date, status="pending"):
    return {"id": "s1", "ticker": "8888.TW", "direction": "buy", "bar_date": bar_date,
            "entry": 100.0, "stop": 95.0, "tp1": 110.0, "tp2": 115.0, "tp3": 125.0,
            "status": status, "r10": None, "r_multiple": None}


def test_stale_ticker_leaves_pending_after_expiry(monkeypatch):
    """有舊 K 棒但訊號日後無新棒，且已逾期＋寬限 → 標 unresolved/no_new_bars，不再卡住。"""
    rows = [_row("2026-10-05")]
    # 只有訊號日（含）之前的 K 棒：停牌／下市後資料源停更
    bars = {"8888.TW": [{"bar_date": "2026-10-03", "open": 99, "high": 101, "low": 98, "close": 100},
                        {"bar_date": "2026-10-05", "open": 99, "high": 101, "low": 98, "close": 100}]}
    res, captured = _run_resolve(monkeypatch, rows, bars)
    assert res["no_new_bars"] == 1
    upd = [q for q, _ in captured if "no_new_bars" in q]
    assert upd and "status='unresolved'" in upd[0]       # 離開 pending 佇列
    assert res["closed"] == 0                             # 不猜測結果、不計入勝率


def test_stale_ticker_still_pending_before_expiry(monkeypatch):
    """尚未逾期者仍留 pending（可能只是今天還沒開盤），不得提早判死。"""
    from datetime import timedelta
    recent = (shadow.datetime.now(shadow.TPE) - timedelta(days=2)).strftime("%Y-%m-%d")
    rows = [_row(recent)]
    bars = {"8888.TW": [{"bar_date": recent, "open": 99, "high": 101, "low": 98, "close": 100}]}
    res, captured = _run_resolve(monkeypatch, rows, bars)
    assert res["no_new_bars"] == 0
    assert not [q for q, _ in captured if "no_new_bars" in q]


def test_mark_sent_initialises_delivered_for_reconciliation(monkeypatch):
    """排入推播時把 delivered 初始化為 0（已確認的保持 1），往後每筆都落在可對帳母體。"""
    captured = []
    import state_store
    monkeypatch.setattr(state_store, "store", _stub_store([], captured))
    monkeypatch.setattr(shadow, "ensure_table", lambda: None)
    shadow.mark_sent(["2330.TW"])
    q = captured[0][0]
    assert "sent=1" in q and "delivered=COALESCE(delivered,0)" in q   # 不覆蓋已確認的 1


def test_delivery_denominator_excludes_rows_without_field():
    """無對帳欄位的舊列不得混入分母：23 筆排入中 20 筆為舊紀錄 → 可對帳母體只有 3 筆。"""
    d = shadow._delivery_summary(sent=23, delivered=3, no_field=20, unconfirmed=0)
    assert d["reconcilable"] == 3 and d["no_field"] == 20
    assert d["confirmed_rate"] == 100.0            # 可對帳的都已確認，不再被舊資料拉低
    assert d["unconfirmed"] == 0
    assert "不納入分母" in d["note"]


def test_delivery_real_miss_still_surfaces():
    """可對帳母體內真的未確認者仍要被看見（不得因本次修正被蓋掉）。"""
    d = shadow._delivery_summary(sent=10, delivered=4, no_field=2, unconfirmed=4)
    assert d["reconcilable"] == 8 and d["unconfirmed"] == 4
    assert d["confirmed_rate"] == 50.0


def test_delivery_summary_backward_compatible():
    """未帶新參數時沿用舊行為（既有呼叫端／報表不受影響）。"""
    d = shadow._delivery_summary(sent=22, delivered=3)
    assert d["sent"] == 22 and d["delivered"] == 3 and d["unknown"] == 19
    assert d["confirmed_rate"] == round(3 / 22 * 100, 1)
