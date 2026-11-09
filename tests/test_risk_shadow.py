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

        def read_meta(self, k):
            # 與真實 store.read_meta 同語意：("ok",v) / ("missing",None)
            return ("ok", store[k]) if k in store else ("missing", None)

        def set_meta(self, k, v):
            store[k] = v
            return True
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


def test_daily_loss_fail_closed_on_db_error(monkeypatch):
    """★ F-07：風控狀態（每日虧損）資料庫讀取失敗時，不可假裝可交易；
    check_daily_loss_limit 回報 unavailable，run_all_checks 將其列為 blocker（擋新單）。"""
    import risk_manager as rm

    class _ErrStore:
        def read_meta(self, k):
            return ("error", None)      # 模擬資料庫讀取失敗
        def get_meta(self, k, default=None):
            return default
        def set_meta(self, k, v):
            return False
    import state_store
    monkeypatch.setattr(state_store, "store", _ErrStore())

    out = rm.check_daily_loss_limit()
    assert out.get("unavailable") is True
    assert out.get("today_loss") is None          # 不謊稱「今日無虧損」

    res = rm.run_all_checks("2330.TW", {"close": 600}, {"daily": {"closes": [1, 2, 3]}},
                            {"index": {"twii": {"chg": 0.1, "source": "ok"},
                                       "vix": {"price": 18, "source": "ok"}}}, active_signals=[])
    assert res["can_signal"] is False             # fail-closed：擋新單
    assert any("風控狀態" in b for b in res["blockers"])


def test_portfolio_hard_limit_blocks_new_signals():
    # ★ F-09：組合「最壞同時停損」總和超過硬上限 → 擋新訊號；介於警告(10%)與硬上限之間 → 只警告不擋
    import risk_manager as rm
    mo = {"index": {"twii": {"chg": 0.1, "source": "ok"}, "vix": {"price": 18, "source": "ok"}},
          "foreign": {"net_buy_twd": 0}, "margin": {"chg_pct": 0}}
    stock = {"close": 600}
    tf = {"daily": {"closes": [100, 100, 100]}}      # 無漲跌停尖波，避免 spike 擋單干擾
    over = [{"risk_pct": 5, "risk_twd": 25000, "position_value": 100000} for _ in range(4)]   # 20% > 15%
    res = rm.run_all_checks("2330.TW", stock, tf, mo, active_signals=over)
    assert res["checks"]["margin"]["exceeded"] is True
    assert res["can_signal"] is False and any("組合總風險" in b for b in res["blockers"])
    warn = [{"risk_pct": 3, "risk_twd": 15000, "position_value": 60000} for _ in range(4)]    # 12%（>10、<15）
    res2 = rm.run_all_checks("2330.TW", stock, tf, mo, active_signals=warn)
    assert res2["checks"]["margin"]["warning"] is True and res2["checks"]["margin"]["exceeded"] is False
    assert res2["can_signal"] is True                # 警告不擋新單


def test_portfolio_risk_basic():
    # 第三方 brief §4#3：組合風險快照——最壞同時停損、曝險、方向、情境壓力測試
    import risk_manager as rm
    rows = [
        {"code": "2330", "suggested_lots": 1000, "entry_price": 600, "stop_loss": 570, "direction": "buy", "sector": "半導體業"},
        {"code": "2317", "suggested_lots": 2000, "entry_price": 100, "stop_loss": 95, "direction": "buy", "sector": "電子零組件業"},
        {"code": "2603", "suggested_lots": 1000, "entry_price": 200, "stop_loss": 210, "direction": "sell", "sector": "航運業"},
    ]
    out = rm.portfolio_risk(rows, balance=1_000_000)
    assert out["ok"] and out["n"] == 3
    # 最壞同時停損 = 1000*30 + 2000*5 + 1000*10 = 50000（5%）
    assert out["worst_case_loss"] == 50000 and out["worst_case_loss_pct"] == 5.0
    # 多單 800k、空單 200k、gross 1,000,000
    assert out["gross_exposure"] == 1_000_000
    assert out["long_pct"] == 80.0 and out["short_pct"] == 20.0 and out["net_pct"] == 60.0
    # 大盤 -5% 情境：-(800k*5%)+(200k*5%) = -30000（-3%）
    s5 = next(s for s in out["scenarios"] if s["drop_pct"] == 5)
    assert s5["pnl_twd"] == -30000 and s5["pnl_pct"] == -3.0
    assert isinstance(out["flags"], list) and out["largest"]["code"] == "2330"


def test_portfolio_risk_empty():
    import risk_manager as rm
    out = rm.portfolio_risk([], balance=1_000_000)
    assert out["ok"] and out["n"] == 0


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
                # 攔截 r_multiple 寫入值。以欄位名定位（不再假設固定位置，因後續可能追加 champion_r 等欄位）。
                saved["q"] = q; saved["vals"] = p
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
    # 以 SET 子句中「帶 ? 的欄位」順序定位 r_multiple 的參數位置（params 依序填入、非 ? 欄位跳過）。
    set_clause = saved["q"].split(" SET ", 1)[1].rsplit(" WHERE ", 1)[0]
    ph_fields = [f.strip() for f in set_clause.split(",") if "?" in f]
    idx = next(i for i, f in enumerate(ph_fields) if f.startswith("r_multiple"))
    net_r = saved["vals"][idx]
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


# ── 2026-10-15 搜集能力提升：snapshot 新增特徵（純由日K陣列計算，無外部相依）──
def test_snapshot_new_collection_features():
    import shadow
    n = 30
    closes = [100.0 + i for i in range(n)]                 # 嚴格遞增
    highs = [c + 0.0 for c in closes]                       # 收在最高 → clv=100
    lows = [c - 2.0 for c in closes]
    opens = [c - 1.0 for c in closes]
    vols = [1000 + i * 100 for i in range(n)]               # 量遞增 → vol_trend5>1
    dates = [f"2026-09-{(i % 28) + 1:02d}" for i in range(n)]
    daily = {"closes": closes, "highs": highs, "lows": lows, "opens": opens,
             "volumes": vols, "dates": dates, "change_pct": 1.0}
    f = shadow.snapshot(daily)
    assert f["up_days"] == n - 1                             # 全程連漲
    assert f["clv"] == 100.0                                 # 收最高
    assert f["dist_ma5"] > 0 and f["ma5_slope"] > 0          # 站上並高於 5 日線、斜率向上
    assert f["vol_trend5"] > 1                               # 近 5 日均量 > 前 5 日
    assert f["above20_days"] >= 1                            # 近期站上 20 日線


def test_snapshot_down_streak_negative():
    import shadow
    n = 12
    closes = [200.0 - i for i in range(n)]                   # 嚴格遞減
    daily = {"closes": closes, "highs": [c + 1 for c in closes], "lows": [c - 1 for c in closes],
             "opens": closes[:], "volumes": [1000] * n,
             "dates": [f"2026-09-{i + 1:02d}" for i in range(n)]}
    f = shadow.snapshot(daily)
    assert f["up_days"] == -(n - 1)                          # 連跌記為負


def test_shadow_simulate_new_metrics():
    """新增的每筆模擬欄位：r1/r20、mae_bars/mfe_bars、tp1_before_sl。"""
    import shadow
    row = {"entry": 100.0, "stop": 90.0, "direction": "buy", "bar_date": "2026-10-01",
           "tp1": 120.0, "tp2": 140.0, "tp3": 160.0}
    # 進場 100（隔日開盤），之後走高到 125（第3根摸到 tp1=120）
    bars = [{"bar_date": "2026-10-01", "open": 99, "high": 101, "low": 98, "close": 100}]
    seq = [(100, 108, 99, 105), (105, 118, 104, 115), (115, 126, 112, 124)]
    for i, (o, h, l, c) in enumerate(seq, start=2):
        bars.append({"bar_date": f"2026-10-{i:02d}", "open": o, "high": h, "low": l, "close": c})
    sim = shadow._simulate(row, bars, expire_days=15)
    assert sim["result"] == "tp1"
    # risk=10（100-90）；r1=隔日收盤105 → (105-100)/10 = +0.5R
    assert sim["r1"] == 0.5
    assert sim["mfe_bars"] is not None and sim["mae_bars"] is not None
    assert sim["tp1_before_sl"] == 1          # 先到 tp1、沒碰停損


def test_shadow_snapshot_new_features():
    """擴充搜集的特徵（純由日K算）：52週位置、均線多頭排列、K棒實體、量能 z。"""
    import shadow
    n = 260
    closes = [100 + i * 0.3 for i in range(n)]          # 穩定上升 → 均線完全多頭、接近高點
    daily = {"closes": closes, "highs": [c + 1 for c in closes], "lows": [c - 1 for c in closes],
             "opens": [c - 0.5 for c in closes], "volumes": [1000 + (i % 7) * 10 for i in range(n)],
             "dates": [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)], "change_pct": 0.3}
    f = shadow.snapshot(daily)
    assert f.get("ma_stack") == 3                        # 5>10>20>60 全成立
    assert f.get("pos52") is not None and f["pos52"] > 90
    assert "body_pct" in f and "maxdd20" in f and "turnover_m" in f
