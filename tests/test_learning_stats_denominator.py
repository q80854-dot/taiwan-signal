"""S-P0-03／S-P1-06：統計分母對齊與指標正名（2026-11-12）。

原本「勝率」以全部列為分母、「平均 R」只以有 R 的列為分母。上傳的歷史 CSV 有 78 筆對照組
（缺停損、無有效 R）被算進前者、排除於後者——兩個數字不是同一批樣本，卻並列顯示。
"""
import learning


def _r(result, r):
    return {"result": result, "r_multiple": r, "r10": None, "mfe_r": None, "mae_r": None,
            "bars_held": None, "tp1_before_sl": None}


def test_invalid_rows_excluded_from_event_rates_and_disclosed():
    rows = [_r("tp1", 1.0), _r("sl", -1.0), _r("sl", -1.0), _r("expired", 0.2),
            _r("sl", None), _r("sl", None)]                      # 兩筆無有效 R（如缺停損的對照組）
    st = learning._stats(rows)
    assert st["n"] == 6 and st["n_valid_r"] == 4 and st["n_invalid"] == 2
    assert st["win_rate"] == 25.0 and st["stop_rate"] == 50.0    # 以 4 筆有效列為分母，不是 6
    assert st["avg_r"] == round((1.0 - 1.0 - 1.0 + 0.2) / 4, 3)  # 與比率同一批樣本


def test_target_touch_rate_is_not_net_positive_rate():
    """觸及 TP1 但扣成本後 ≤0、到期出場卻為正：事件標籤與淨損益必須分開回報。"""
    rows = [_r("tp1", -0.05), _r("tp1", 0.8), _r("expired", 0.3), _r("expired", 0.1), _r("sl", -1.0)]
    st = learning._stats(rows)
    assert st["target_touch_rate"] == 40.0       # 2/5 觸及停利
    assert st["net_positive_rate"] == 60.0       # 3/5 淨 R 為正
    assert st["win_rate"] == st["target_touch_rate"]             # 舊鍵沿用＝觸及停利率（相容）


def test_per_trade_r_stability_alias_and_all_invalid_group():
    st = learning._stats([_r("tp1", 1.0), _r("sl", -1.0), _r("tp1", 0.5)])
    assert st["per_trade_r_stability"] == st["sharpe"]           # 每筆 R 穩定度，非年化帳戶 Sharpe
    empty = learning._stats([_r("sl", None)])
    assert empty["n"] == 1 and empty["n_valid_r"] == 0
    assert empty["win_rate"] is None and empty["avg_r"] is None  # 沒有有效樣本就不給數字
