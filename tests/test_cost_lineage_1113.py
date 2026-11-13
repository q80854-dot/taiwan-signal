"""2026-11-13：實際手續費／ETF 證交稅、策略版本不受掃描暫時門檻影響。"""
import config
import strategy_version as sv


def test_config_hash_ignores_transient_min_score_bump():
    base = sv.config_hash()
    orig = config.SIGNAL_THRESHOLDS["min_score"]
    try:
        config.SIGNAL_THRESHOLDS["min_score"] = orig + 5          # 掃描在財報季／大盤偏弱時的暫時調整
        assert sv.config_hash() == base
    finally:
        config.SIGNAL_THRESHOLDS["min_score"] = orig


def test_sell_tax_rate_etf_vs_stock():
    assert config.sell_tax_rate("00631L.TW") == 0.001
    assert config.sell_tax_rate("0050") == 0.001
    assert config.sell_tax_rate("1101.TW") == 0.003
    assert config.sell_tax_rate(None) == 0.003                   # 未知代號用股票稅率（保守）


def test_trade_cost_matches_broker_statement():
    from signal_engine import calc_trade_cost
    # 實際成交明細：元大台灣50正2 賣出 2,000 股 @26.48 → 成交 52,960、證交稅 52（0.1%）
    c = calc_trade_cost(26.48, 2000, "sell", "00631L.TW")
    assert c["tax"] == 53                                        # 52.96：券商無條件捨去為 52，系統四捨五入為 53
    # 台泥賣出 300 股 @26.75 → 8,025、證交稅 24（0.3%）
    assert calc_trade_cost(26.75, 300, "sell", "1101.TW")["tax"] == 24
    # 小額：811 元的買單，手續費實收 1 元（原本系統最低 20 元）
    assert calc_trade_cost(16.23, 50, "buy", "009816.TW")["commission"] == 1


def test_shadow_and_backtest_costs_use_etf_rate():
    import shadow
    import backtester as bt
    etf, stk = shadow._shadow_staged_pnl_factory("00631L.TW"), shadow._shadow_staged_pnl_factory("2330.TW")
    assert etf(100, 100, "buy", 1000) > stk(100, 100, "buy", 1000)     # 平盤出場：ETF 成本較低
    assert bt.calc_tw_pnl(100, 100, "buy", 1000, code="0050") > bt.calc_tw_pnl(100, 100, "buy", 1000, code="2330")
    assert shadow._shadow_staged_pnl_factory()(100, 100, "buy", 1000) == stk(100, 100, "buy", 1000)


def test_alias_maps_bumped_hash_to_approved_version():
    import shadow
    assert shadow.CONFIG_VER_ALIASES["3b6c032b4106"] == "0c49884d59de"
