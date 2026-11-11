"""SH09（第三階段審查）：識別／決策衍生欄與技術因子分類。

絕對價位、部位股數、成本與決策輸出（entry_price/stop_loss/tp*/suggested_shares/atr…）
由決策本身決定，非 alpha 預測因子。它們仍照算 IC，但標記 derived=True、前端另列，
且在排序時排在 alpha 因子之後，不擠掉技術因子。
"""
import learning as L


def test_is_derived_flags():
    # 識別／決策衍生欄
    for k in ["entry_price", "current_price", "stop_loss", "tp1", "tp2", "tp3",
              "atr", "sl_pct", "rr1", "suggested_shares", "suggested_lots",
              "risk_twd", "position_value", "size_cat", "pnl_twd"]:
        assert L._is_derived(k) is True, k
    # 技術／基本面 alpha 因子
    for k in ["ret20", "dist_ma20", "rsi_value", "adx_value", "vol_z20",
              "pos52", "ma_stack", "sec_rel20"]:
        assert L._is_derived(k) is False, k


def _rows(n):
    out = []
    for i in range(n):
        out.append({"f": {"ret20": (i % 7) - 3.0, "entry_price": 10.0 + i},
                    "r_multiple": ((i * 7) % 11) / 5.0 - 1.0,
                    "bar_date": f"2026-10-{(i % 6) + 1:02d}",
                    "direction": "buy"})
    return out


def test_ic_one_tags_derived_and_sorts_alpha_first():
    rep = L._ic_one(_rows(40), "做多")
    facs = rep.get("factors") or []
    assert facs, "應至少有一個因子"
    names = [f["factor"] for f in facs]
    assert "entry_price" in names and "ret20" in names
    for f in facs:
        assert "derived" in f
        assert f["derived"] == L._is_derived(f["factor"])
    # alpha（derived=False）必須排在所有 derived 之前
    flags = [f["derived"] for f in facs]
    assert flags == sorted(flags), "alpha 因子應排在 derived 之前"
    assert L._is_derived("entry_price") and not L._is_derived("ret20")


def test_daily_ic_side_tags_derived():
    # 6 天 × 每天 5 檔（>min_breadth=4、>min_days=5）
    rows = []
    for day in range(6):
        for j in range(5):
            rows.append({"f": {"ret20": j - 2.0, "entry_price": 10.0 + j},
                         "r_multiple": (j * 3 % 5) - 2.0,
                         "bar_date": f"2026-10-{day + 1:02d}"})
    side = L._daily_ic_side(rows, "r_multiple", "做多")
    facs = side.get("factors") or []
    assert facs, "每日 ICIR 應至少產出一個因子"
    for f in facs:
        assert f["derived"] == L._is_derived(f["factor"])
    flags = [f["derived"] for f in facs]
    assert flags == sorted(flags), "alpha 因子應排在 derived 之前"


def test_daily_block_has_span():
    rows = []
    for day in range(6):
        for j in range(5):
            rows.append({"f": {"ret20": j - 2.0}, "r_multiple": (j % 3) - 1.0,
                         "bar_date": f"2026-10-{day + 1:02d}"})
    block = L._daily_ic_block(rows, [])
    span = block.get("span") or {}
    assert span.get("first_day") == "2026-10-01"
    assert span.get("last_day") == "2026-10-06"
    assert span.get("n_days") == 6
