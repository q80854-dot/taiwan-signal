"""SH07（第三階段審查）：蒙地卡羅誠實標示。

_mc_equity 原稱「大數據」「獲利機率」，易被解讀成未來真實機率。改為本期經驗分布 IID 重抽樣，
附 method/note；prob_profit 是「本次模擬正值比例」，並回傳 sims_positive 讓 0% 能誠實呈現。
"""
import learning as L


def _rows(result, rm, n):
    return [{"result": result, "r_multiple": rm} for _ in range(n)]


def test_mc_all_positive():
    out = L._mc_equity(_rows("tp1", 1.0, L.MIN_N + 5), n_sims=100)
    assert out["ready"] is True
    assert out["prob_profit"] == 100.0
    assert out["sims_positive"] == 100
    assert out["method"] == "iid_resample_current"
    assert "非未來真實機率" in out["note"]


def test_mc_all_negative_shows_zero_positive():
    out = L._mc_equity(_rows("sl", -1.0, L.MIN_N + 5), n_sims=100)
    assert out["prob_profit"] == 0.0
    assert out["sims_positive"] == 0        # 本次模擬無正值（非未來機率=0）


def test_mc_insufficient():
    out = L._mc_equity(_rows("tp1", 1.0, 3), n_sims=100)
    assert out["ready"] is False
