"""策略模型／影子追蹤修正的回歸測試（2026-10-09）。"""
from datetime import datetime


def test_daily_loss_persists_across_restart(monkeypatch, tmp_path):
    """每日虧損熔斷要寫進資料庫（meta），程序重啟（記憶體歸零）後仍能讀回累計值。"""
    import risk_manager as rm
    store = {}
    monkeypatch.setattr(rm, "_daily_loss", {"date": "", "loss_twd": 0.0, "signal_count": 0}, raising=False)

    class _FakeStore:
        def get_meta(self, k, default=None):
            return store.get(k, default)

        def set_meta(self, k, v):
            store[k] = v
    import state_store
    monkeypatch.setattr(state_store, "store", _FakeStore())

    today = datetime.now(rm.TZ_TAIPEI).strftime("%Y-%m-%d")
    rm.record_signal_loss(-5000)
    rm.record_signal_loss(-3000)
    assert store["risk_daily_loss"]["date"] == today
    assert abs(store["risk_daily_loss"]["loss_twd"] - 8000) < 1e-6
    # 模擬「重啟」：記憶體歸零，但資料庫還在 → check 仍看得到今日虧損
    rm._daily_loss.update({"date": "", "loss_twd": 0.0, "signal_count": 0})
    out = rm.check_daily_loss_limit()
    assert out["today_loss"] == 8000


def test_shadow_simulate_enters_at_next_open():
    """影子結算進場價要用訊號日後第一根開盤價（與實盤一致），而非訊號日收盤。"""
    import shadow
    row = {"entry": 100.0, "stop": 95.0, "direction": "buy", "bar_date": "2026-10-08",
           "tp1": 110.0, "tp2": 115.0, "tp3": 125.0}
    # 隔日開高 105（跳空），當天就摸到 tp1=110；若用收盤 100 當進場 R 會較高，用開盤 105 會較低
    bars = [
        {"bar_date": "2026-10-08", "open": 99, "high": 101, "low": 98, "close": 100},
        {"bar_date": "2026-10-09", "open": 105, "high": 112, "low": 104, "close": 111},
    ]
    sim = shadow._simulate(row, bars, expire_days=15)
    assert sim["entry_fill"] == 105
    assert sim["result"] == "tp1" and sim["exit_price"] == 110.0
    # R 以規劃風險 |100-95|=5 為單位，分子用實際開盤進場 105：(110-105)/5 = 1.0（毛 R）
    risk = abs(row["entry"] - row["stop"])
    assert abs((sim["exit_price"] - sim["entry_fill"]) / risk - 1.0) < 1e-6


def test_shadow_r_multiple_is_net_of_costs(monkeypatch, tmp_path):
    """resolve_pending 寫回的 r_multiple 應扣掉來回手續費＋證交稅（與實盤淨損益同口徑），略低於毛 R。"""
    import shadow
    from config import COMMISSION_RATE, TAX_RATE_SELL
    rows = [{"id": "x1", "ticker": "9999.TW", "direction": "buy", "bar_date": "2026-10-08",
             "entry": 100.0, "stop": 95.0, "tp1": 110.0, "tp2": 115.0, "tp3": 125.0,
             "status": "pending", "r10": None, "r_multiple": None}]
    bars = {"9999.TW": [
        {"bar_date": "2026-10-08", "open": 99, "high": 101, "low": 98, "close": 100},
        {"bar_date": "2026-10-09", "open": 105, "high": 112, "low": 104, "close": 111}]}
    saved = {}

    class _Store:
        def _conn(self): return self

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def executescript(self, q): return self

        def execute(self, q, p=()):
            if q.strip().startswith("SELECT"):
                self._rows = [dict(r) for r in rows]; return self
            if q.strip().startswith("UPDATE"):
                # 攔截 r_multiple 寫入值（倒數第二個參數為 r_multiple，最後是 id）
                saved["vals"] = p
            return self

        def fetchall(self): return getattr(self, "_rows", [])
    import state_store
    monkeypatch.setattr(state_store, "store", _Store())
    monkeypatch.setattr(shadow, "ensure_table", lambda: None)
    monkeypatch.setattr(shadow, "_load_bars", lambda tickers, since: bars)
    monkeypatch.setattr(shadow, "_rescale", lambda row, bars: (dict(row), 1.0))
    from config import SLIPPAGE_RATE
    res = shadow.resolve_pending()
    assert res["closed"] == 1
    net_r = saved["vals"][-2]
    # 含滑價＋來回手續費＋證交稅（買方；借券費=0）的淨 R，需 < 毛 R(1.0) 但仍為正
    eff_fill = 105 * (1 + SLIPPAGE_RATE); eff_exit = 110 * (1 - SLIPPAGE_RATE)
    gross_r = (eff_exit - eff_fill) / 5.0
    cost_r = (eff_fill * COMMISSION_RATE + eff_exit * COMMISSION_RATE + eff_exit * TAX_RATE_SELL) / 5.0
    assert abs(net_r - (gross_r - cost_r)) < 1e-3 and 0 < net_r < 1.0


def test_calc_tw_pnl_slippage_and_short_borrow():
    """calc_tw_pnl 納入滑價與放空借券費：同樣進出場，做多淨損益應低於無滑價毛額；放空持有越久借券費越高。"""
    from backtester import calc_tw_pnl
    from config import SLIPPAGE_RATE, SHORT_BORROW_ANNUAL
    # 做多：買 100 賣 110、1000 股。毛利 10000，滑價讓買貴賣賤，淨利應明顯低於 10000
    pnl_long = calc_tw_pnl(100, 110, "buy", 1000)
    assert 0 < pnl_long < 10000
    # 放空借券費隨持有天數增加：持有 30 天的淨損益應低於持有 1 天
    short_1d = calc_tw_pnl(110, 100, "sell", 1000, days_held=1)
    short_30d = calc_tw_pnl(110, 100, "sell", 1000, days_held=30)
    assert short_30d < short_1d
    # 借券費差額約等於 賣出腳價 × 年化 × (30-1)/365
    sell_leg = 110 * (1 - SLIPPAGE_RATE) * 1000
    assert abs((short_1d - short_30d) - sell_leg * SHORT_BORROW_ANNUAL * 29 / 365) < 1.0
