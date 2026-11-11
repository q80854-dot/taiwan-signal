"""SH10（第三階段審查）：蒙地卡羅 p 值解析度下界。

_boot_diff 原本 p = 2*min(count/B)，極端情形 count=0 → p=0（誤導成機率為零）。
改採 (b+1)/(B+1) 加一校正：p 下界 ≈ 2/(B+1)，不會真的為 0。
"""
import learning as L


def _rows(vals, days):
    out = []
    for i, v in enumerate(vals):
        out.append({"r_multiple": v, "bar_date": days[i % len(days)]})
    return out


def test_boot_diff_p_never_zero_on_extreme_separation():
    days = ["2026-10-01", "2026-10-02", "2026-10-03"]
    a = _rows([3.0, 3.1, 2.9, 3.2, 3.0, 2.8], days)   # 明顯較高
    b = _rows([-2.0, -2.1, -1.9, -2.2, -2.0, -1.8], days)  # 明顯較低
    diff, p, z = L._boot_diff(a, b)
    assert diff is not None and diff > 0
    assert p > 0                                       # 不再是 0
    assert p <= 0.01                                   # 落在解析度下界附近（≈2/(B+1)），非真正機率為零


def test_boot_diff_insufficient_returns_one():
    diff, p, z = L._boot_diff([{"r_multiple": 1.0, "bar_date": "2026-10-01"}],
                              [{"r_multiple": 0.0, "bar_date": "2026-10-01"}])
    assert diff is None and p == 1.0
