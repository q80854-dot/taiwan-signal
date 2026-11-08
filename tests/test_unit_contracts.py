"""單位契約測試（治理 TS-DATA-001 / TS-TEST-001，2026-11-08）。

把部位計算的「單位不變量」寫成硬性契約：任何未來改動只要破壞這些關係（例如又把股/張
搞混、風險預算失聯、部位上限失效），這裡就會紅。對照 docs/ai-governance/DATA_DICTIONARY.md。

全部離線、確定性，不打網路。
"""
import math

import pytest

from config import MAX_RISK_PER_TRADE, MAX_SIMULTANEOUS_POSITIONS, SHARES_PER_LOT, ACCOUNT_BALANCE_TWD
from signal_engine import calc_position_size

# (price, stop_loss, size_cat)：涵蓋高價股（原始風控 bug 來源）、低價股、ETF、寬/窄停損
CASES = [
    (1000.0, 950.0, "大型股"),   # 高價股、5% 停損
    (1000.0, 900.0, "大型股"),   # 高價股、寬停損（單股風險大）
    (50.0, 47.5, "中型股"),      # 低價中型股
    (18.0, 17.5, "小型股"),      # 低價小型股、窄停損
    (150.0, 142.0, "ETF"),       # ETF
]


@pytest.mark.parametrize("price,stop,cat", CASES)
def test_position_unit_invariants(price, stop, cat):
    pos = calc_position_size(price, stop, balance=ACCOUNT_BALANCE_TWD, size_cat=cat)
    sh = pos["shares"]
    if sh < 1:
        pytest.skip("此參數換算不足 1 股、不進場（本身即合法結果）")

    # 1) 張＝股÷1000
    assert pos["lots"] == round(sh / SHARES_PER_LOT, 3)
    # 2) 風險金額＝股數 × 每股風險（|進場−停損|）
    assert pos["risk_twd"] == round(sh * abs(price - stop), 0)
    # 3) 部位市值＝股數 × 進場價
    assert pos["position_value"] == round(sh * price, 0)
    # 4) 風險%／保證金% 與金額自洽
    assert pos["risk_pct"] == round(pos["risk_twd"] / ACCOUNT_BALANCE_TWD * 100, 2)
    assert pos["margin_pct"] == round(pos["position_value"] / ACCOUNT_BALANCE_TWD * 100, 1)


@pytest.mark.parametrize("price,stop,cat", CASES)
def test_risk_never_exceeds_cap(price, stop, cat):
    """核心風控保證：單筆風險永不超過 MAX_RISK_PER_TRADE（高價股也不例外——這正是當初改零股的原因）。"""
    pos = calc_position_size(price, stop, balance=ACCOUNT_BALANCE_TWD, size_cat=cat)
    if pos["shares"] < 1:
        pytest.skip("不進場")
    # 容忍四捨五入（risk_twd 以 round(...,0) 回傳）：不得超過 2% 預算 + 一股的風險緩衝
    max_risk = ACCOUNT_BALANCE_TWD * MAX_RISK_PER_TRADE
    assert pos["risk_twd"] <= max_risk + abs(price - stop) + 1


@pytest.mark.parametrize("price,stop,cat", CASES)
def test_position_value_within_cap(price, stop, cat):
    """部位市值不得超過『資金 ÷ 同時持倉上限』（2026-10-28 修正：避免 5 檔合計爆資金）。"""
    pos = calc_position_size(price, stop, balance=ACCOUNT_BALANCE_TWD, size_cat=cat)
    if pos["shares"] < 1:
        pytest.skip("不進場")
    cap = ACCOUNT_BALANCE_TWD / max(1, MAX_SIMULTANEOUS_POSITIONS)
    assert pos["position_value"] <= cap + price  # 容忍不足一股的無條件捨去邊界


def test_zero_position_on_invalid_stop():
    """停損距離為 0 或價格非法 → 不進場、全為 0（fail-safe）。"""
    pos = calc_position_size(100.0, 100.0, balance=ACCOUNT_BALANCE_TWD)
    assert pos["shares"] == 0 and pos["risk_twd"] == 0 and pos["position_value"] == 0


def test_liquidity_cap_limits_shares():
    """流動性上限：部位不超過當日成交量 10%（避免自造滑價）。"""
    # avg_volume_lots=1 張＝1000 股，10% ＝ 100 股上限
    pos = calc_position_size(50.0, 47.5, balance=ACCOUNT_BALANCE_TWD, size_cat="中型股", avg_volume_lots=1)
    assert pos["shares"] <= 100
    assert pos.get("liquidity_capped") is True
