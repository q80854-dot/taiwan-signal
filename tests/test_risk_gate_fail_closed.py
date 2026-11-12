"""Q-P0-06：風控狀態未知時 fail-closed（2026-11-12）。

原本掃描前的風控檢查只判斷 exceeded：
  - 當日虧損狀態讀取失敗（unavailable）→ 照常發訊號；
  - 持倉清單讀取失敗 → get_pending_signals 回空清單，被當成「0 個持倉」→ 照常發訊號；
  - 檢查本身丟例外 →「不影響本次掃描繼續」→ 照常發訊號。
審查報告以為下游的 risk_manager.run_all_checks 會擋，但該函式在正式流程從未被呼叫。
"""
import re

import risk_manager
import scanner as scanner_mod
from scanner import TWScanEngine


class _Store:
    def __init__(self, status="ok", rows=None, closed=None, boom=False):
        self._status, self._rows, self._closed, self._boom = status, rows or [], closed or [], boom

    def read_pending_signals(self, limit=500):
        if self._boom:
            raise RuntimeError("db down")
        return self._status, list(self._rows)

    def get_pending_signals(self, limit=500):
        return self.read_pending_signals(limit)[1]

    def get_closed_trades(self, limit=200):
        return list(self._closed)


def _gate(monkeypatch, store, daily=None):
    monkeypatch.setattr(risk_manager, "check_daily_loss_limit",
                        lambda: daily or {"exceeded": False, "today_loss": 0})
    return TWScanEngine()._risk_gate(store)


def test_normal_state_allows_new_signals(monkeypatch):
    g = _gate(monkeypatch, _Store())
    assert g["skip"] is False and g["risk_unknown"] is False and g["msgs"] == []


def test_daily_loss_state_unavailable_blocks_new_signals(monkeypatch):
    g = _gate(monkeypatch, _Store(), daily={"exceeded": False, "unavailable": True, "message": "🔴 風控狀態暫時無法讀取"})
    assert g["skip"] is True and g["risk_unknown"] is True and g["hc_mode"] is False
    assert any("無法讀取" in m for m in g["msgs"])


def test_pending_positions_unreadable_is_not_treated_as_zero_positions(monkeypatch):
    g = _gate(monkeypatch, _Store(status="error"))
    assert g["skip"] is True and g["risk_unknown"] is True
    assert any("未平倉部位暫時無法讀取" in m for m in g["msgs"])


def test_risk_check_exception_blocks_new_signals(monkeypatch):
    g = _gate(monkeypatch, _Store(boom=True))
    assert g["skip"] is True and g["risk_unknown"] is True and g["hc_mode"] is False
    assert any("風控檢查失敗" in m for m in g["msgs"])


def test_high_conf_exception_only_when_risk_state_known(monkeypatch):
    """極高信心例外：只在「單純持倉滿額、風險狀態已知」時才可能開啟；狀態未知一律關閉。"""
    full = [{"ticker": f"T{i}.TW"} for i in range(risk_manager.MAX_SIMULTANEOUS_POSITIONS)]
    perfect = [{"pnl_pct": 1.0}] * risk_manager.HC_MIN_TRADES
    g = _gate(monkeypatch, _Store(rows=full, closed=perfect))
    assert g["skip"] is True and g["hc_mode"] is True and g["risk_unknown"] is False
    g2 = _gate(monkeypatch, _Store(rows=full, closed=perfect),
               daily={"exceeded": False, "unavailable": True, "message": "x"})
    assert g2["skip"] is True and g2["hc_mode"] is False            # 風險狀態未知 → 例外不適用
    g3 = _gate(monkeypatch, _Store(rows=full, closed=perfect), daily={"exceeded": True, "message": "虧損達上限"})
    assert g3["hc_mode"] is False                                   # 當日虧損達上限 → 例外不適用


def test_state_store_distinguishes_read_failure_from_empty(monkeypatch):
    from state_store import store
    st, rows = store.read_pending_signals()
    assert st == "ok" and isinstance(rows, list)

    class _Bad:
        def __enter__(self): raise RuntimeError("db down")
        def __exit__(self, *a): return False
    monkeypatch.setattr(store, "_conn", lambda: _Bad())
    assert store.read_pending_signals() == ("error", [])
    assert store.get_pending_signals() == []                        # 舊介面相容（顯示用）


def test_scan_wiring_uses_gate_and_clears_signals_when_positions_unreadable():
    """接線檢查：掃描主流程使用 _risk_gate；讀不到既有持倉時清空新訊號（不以空清單當成沒有持倉）。
    另確認 premarket／close／手動皆走同一個 _run_daily_scan_impl（單一入口，不會繞過閘門）。"""
    src = open(scanner_mod.__file__, encoding="utf-8").read()
    impl = src[src.index("def _run_daily_scan_impl"):]
    assert "gate = self._risk_gate(store)" in impl and 'skip_new_signals = gate["skip"]' in impl
    m = re.search(r"_pst, _pending = store\.read_pending_signals\(\).*?all_signals = \[\]", impl, re.S)
    assert m and "fail-closed" in m.group(0)
    assert "不影響本次掃描繼續" not in impl[:impl.index("# 2. 品種清單")]   # 舊的 fail-open 分支已移除
    assert src.count("def _run_daily_scan_impl") == 1 and "self._run_daily_scan_impl()" in src
