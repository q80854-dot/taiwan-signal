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
        "2026-06-30": {"revenue": 2200.0, "gross": 1100.0, "net_income": 460.0, "eps": 4.6},
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


def test_build_empty_when_no_rows():
    assert FM._build({}, {}) is None
