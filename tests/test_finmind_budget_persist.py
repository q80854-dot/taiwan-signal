"""FinMind 每小時額度保護線跨重啟延續（2026-11-12 系統檢查以線上日誌確認）。

線上證據：今日 4 次「finmind：已達額度上限，熔斷 15 分鐘」，皆在部署後開機預熱時發生。
根因：_calls（最近一小時請求時間）只在記憶體，重啟歸零 → budget_left() 永遠顯示全滿，
90% 保護線失效，直接撞 FinMind 伺服器真實上限而被熔斷。
"""
import time

import finmind_data as fm


class _Meta:
    def __init__(self): self.box = {}
    def get_meta(self, k, default=None): return self.box.get(k, default)
    def set_meta(self, k, v): self.box[k] = v; return True


def _restart():
    """模擬重啟：清空記憶體狀態，下一次呼叫會重新從資料庫載入。"""
    fm._calls.clear()
    fm._boot_loaded = False


def test_budget_survives_restart(monkeypatch):
    meta = _Meta()
    import state_store
    monkeypatch.setattr(state_store, "store", meta)
    _restart()
    fm._load_block_once()
    now = time.time()
    for i in range(50):
        with fm._lock:
            fm._calls.append(now - i)
    fm._calls_persist()
    assert fm.budget_left() == fm.BUDGET - 50

    _restart()                                           # 部署／重啟
    assert fm.budget_left() == fm.BUDGET                 # 舊行為：歸零後誤以為全滿
    fm._load_block_once()
    assert fm.budget_left() == fm.BUDGET - 50            # 新行為：延續一小時內實際用量
    _restart()


def test_entries_older_than_one_hour_are_dropped(monkeypatch):
    meta = _Meta()
    import state_store
    monkeypatch.setattr(state_store, "store", meta)
    now = time.time()
    import json
    meta.set_meta("fm:calls_1h", json.dumps([now - 4000, now - 3700, now - 10, now - 5]))
    _restart()
    fm._load_block_once()
    assert len(fm._calls) == 2                           # 超過一小時的不計入額度
    assert list(fm._calls) == sorted(fm._calls)          # 維持時間遞增，淘汰邏輯才正確
    _restart()


def test_restore_failure_does_not_break(monkeypatch):
    class _Bad:
        def get_meta(self, k, default=None): return "not-json"
        def set_meta(self, k, v): raise RuntimeError("db down")
    import state_store
    monkeypatch.setattr(state_store, "store", _Bad())
    _restart()
    fm._load_block_once()                                # 壞資料不拋例外
    fm._calls_persist()                                  # DB 寫入失敗不拋例外
    assert fm.budget_left() == fm.BUDGET
    _restart()
