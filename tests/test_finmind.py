"""FinMind 財報整合（2026-10-11）：綜合損益表單季還原、比率計算、無 token 優雅降級。
以假資料驗證解析邏輯（不打外部 api.finmindtrade.com）。"""
import finmind as FM


def test_disabled_without_token(monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    assert FM.enabled() is False
    assert FM.financial_indicators("2330") is None


def test_period_and_match():
    assert FM._period("2026-03-31") == "2026Q1"
    assert FM._period("2026-06-30") == "2026Q2"
    assert FM._period("2026-12-31") == "2026Q4"
    assert FM._match("Revenue", "revenue") is True
    assert FM._match("CostOfGoodsSold", "revenue") is False      # 排除成本
    assert FM._match("GrossProfit", "gross") is True


def test_decumulate_within_fiscal_year():
    # 損益累計：Q1=100, Q2=250(半年), Q3=400, Q4=600 → 單季 100/150/150/200；跨年重置
    byd = {
        "2025-03-31": {"revenue": 100.0}, "2025-06-30": {"revenue": 250.0},
        "2025-09-30": {"revenue": 400.0}, "2025-12-31": {"revenue": 600.0},
        "2026-03-31": {"revenue": 120.0},
    }
    q = FM._decumulate(byd, "revenue")
    assert q["2025-03-31"] == 100.0
    assert q["2025-06-30"] == 150.0
    assert q["2025-09-30"] == 150.0
    assert q["2025-12-31"] == 200.0
    assert q["2026-03-31"] == 120.0            # 新年度 Q1 取原值


def test_build_ratios():
    # 損益（累計）+ 資產負債（時點）→ 單季毛利率/淨利率/ROE/流動比率
    inc = {
        "2026-03-31": {"revenue": 1000.0, "gross": 500.0, "net_income": 200.0, "eps": 2.0},
        "2026-06-30": {"revenue": 1200.0, "gross": 600.0, "net_income": 260.0, "eps": 2.6},
    }
    bs = {
        "2026-03-31": {"equity": 4000.0, "assets": 8000.0, "cur_assets": 3000.0, "cur_liab": 1500.0},
        "2026-06-30": {"equity": 4200.0, "assets": 8400.0, "cur_assets": 3200.0, "cur_liab": 1600.0},
    }
    out = FM._build(inc, bs)
    g = {r["period"]: r["value"] for r in out["gross"]}
    assert g["2026Q1"] == 50.0                 # 500/1000
    assert g["2026Q2"] == 50.0                 # (1100-500)/(2200-1000)=600/1200
    net = {r["period"]: r["value"] for r in out["net"]}
    assert net["2026Q1"] == 20.0               # 200/1000
    eps = {r["period"]: r["value"] for r in out["eps"]}
    assert eps["2026Q2"] == 2.6                # 4.6-2.0 單季
    roe = {r["period"]: r["value"] for r in out["roe"]}
    assert roe["2026Q1"] == 5.0                # 200/4000
    cr = {r["period"]: r["value"] for r in out["current_ratio"]}
    assert cr["2026Q1"] == 200.0               # 3000/1500
    er = {r["period"]: r["value"] for r in out["equity_ratio"]}
    assert er["2026Q1"] == 50.0                # 4000/8000


def test_build_fcf_single_quarter():
    inc = {"2026-03-31": {"revenue": 1000.0}, "2026-06-30": {"revenue": 2000.0}}
    # 現金流累計：OCF Q1=300、H1=700 → 單季 300/400；capex Q1=-100、H1=-250 → 單季 -100/-150
    cf = {"2026-03-31": {"ocf": 300.0, "capex": -100.0},
          "2026-06-30": {"ocf": 700.0, "capex": -250.0}}
    out = FM._build(inc, {}, cf)
    fcf = {r["period"]: r["value"] for r in out["fcf"]}
    assert fcf["2026Q1"] == 200.0          # 300 - |−100|
    assert fcf["2026Q2"] == 250.0          # 400 - |−150|


def test_news_disabled_without_token(monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    assert FM.news("2330") is None


def test_news_parse(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "x")
    FM._cache.clear()
    rows = [{"date": "2026-10-10", "title": "台積電法說會", "source": "鉅亨網", "link": "http://a"},
            {"date": "2026-10-11", "title": "外資調高目標價", "source": "工商", "link": "http://b"},
            {"date": "2026-10-09", "title": "", "source": "x", "link": "http://c"}]
    monkeypatch.setattr(FM, "_fetch", lambda ds, cid, start: rows)
    out = FM.news("2330")
    assert len(out) == 2                    # 空標題被濾掉
    assert out[0]["date"] == "2026-10-11"   # 由新到舊
    assert out[0]["title"] == "外資調高目標價"


def test_build_empty_when_no_rows():
    assert FM._build({}, {}) is None


# ── 三大法人（個股）：2026-10-15，補上櫃與上市備援顯示 ──
def test_institutional_parse(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "x")
    FM._cache.clear()
    rows = [
        {"date": "2026-10-14", "name": "Foreign_Investor", "buy": 5_000_000, "sell": 1_000_000},
        {"date": "2026-10-14", "name": "Investment_Trust", "buy": 0, "sell": 500_000},
        {"date": "2026-10-14", "name": "Dealer_self", "buy": 300_000, "sell": 200_000},
        {"date": "2026-10-14", "name": "Dealer_Hedging", "buy": 50_000, "sell": 0},
        {"date": "2026-10-14", "name": "Foreign_Dealer_Self", "buy": 0, "sell": 0},
        {"date": "2026-10-13", "name": "Dealer_self", "buy": 100_000, "sell": 0},  # 無外資/投信 → 整天略過
    ]
    monkeypatch.setattr(FM, "_fetch", lambda ds, cid, start: rows)
    out = FM.institutional("6488")
    assert len(out) == 1 and out[0]["date"] == "2026-10-14"
    r = out[0]
    assert r["foreign"] == 4000 and r["trust"] == -500 and r["dealer"] == 150
    assert r["total"] == 3650       # (4,000,000 - 500,000 + 150,000)/1000


def test_institutional_unknown_names_skip(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "x")
    FM._cache.clear()
    rows = [{"date": "2026-10-14", "name": "SomethingElse", "buy": 1, "sell": 0}]
    monkeypatch.setattr(FM, "_fetch", lambda ds, cid, start: rows)
    assert FM.institutional("2330") is None     # 認不出機構名稱 → 不輸出（不臆測）


def test_institutional_disabled_without_token(monkeypatch):
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    assert FM.institutional("2330") is None


# ── 2026-10-16 額度熔斷 + DB 快取回退 ──
def test_quota_err_detection():
    assert FM._is_quota_err(402, {}) is True
    assert FM._is_quota_err(429, {}) is True
    assert FM._is_quota_err(200, {"msg": "Requests reach the upper limit"}) is True
    assert FM._is_quota_err(200, {}) is False


def test_circuit_open_short_circuits_fetch(monkeypatch):
    called = []
    monkeypatch.setattr(FM.requests, "get", lambda *a, **k: called.append(1))
    monkeypatch.setattr(FM, "_cooldown_until", FM.time.time() + 100)
    assert FM.circuit_open() is True
    assert FM._fetch("X", "Y", "2020-01-01") == []   # 熔斷中直接回空
    assert not called, "熔斷中不應真的連線"


def test_fetch_trips_breaker_on_quota(monkeypatch):
    monkeypatch.setattr(FM, "_cooldown_until", 0.0)
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)

    class _R:
        status_code = 402
        def json(self): return {"status": 402, "msg": "Requests reach the upper limit"}
        ok = False
    monkeypatch.setattr(FM.requests, "get", lambda *a, **k: _R())
    assert FM._fetch("X", "Y", "2020-01-01") == []
    assert FM.circuit_open() is True       # 一看到額度上限就熔斷
    monkeypatch.setattr(FM, "_cooldown_until", 0.0)   # 還原


def test_financials_db_fallback_when_blocked(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "x")
    FM._cache.clear()
    monkeypatch.setattr(FM, "_cooldown_until", 0.0)
    monkeypatch.setattr(FM, "_fetch", lambda ds, cid, start: [])     # live 全空（模擬額度滿）
    saved = {"eps": [{"period": "2026Q2", "value": 12.3}]}
    monkeypatch.setattr(FM, "_db_get", lambda k: {"at": FM.time.time(), "data": saved})
    out = FM.financial_indicators("2330")
    assert out == saved, "live 抓不到時應回退 DB 最近一次正確季報"


def test_institutional_db_fallback_when_blocked(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "x")
    FM._cache.clear()
    monkeypatch.setattr(FM, "_fetch", lambda ds, cid, start: [])
    saved = [{"date": "2026-10-15", "foreign": 100, "trust": 5, "dealer": 1, "total": 106}]
    monkeypatch.setattr(FM, "_db_get", lambda k: {"at": FM.time.time(), "data": saved})
    assert FM.institutional("2330") == saved
