"""SH06（第三階段審查）：規則歸因依(原因,方向)分組，措辭描述性。

_rules 原本只依 reject 原因分組、取第一筆方向代表整組——同一原因含多空時方向會標錯。
且「這條規則在保護你」是對 0 比較的因果宣稱，應改描述性。
"""
import learning as L


def _r(reject, direction, result, rm):
    return {"f": {"reject": reject}, "direction": direction, "result": result, "r_multiple": rm}


def test_rules_split_by_direction():
    # 同一原因 "vol" 同時有做多與做空 → 應產生兩組、方向各自正確，不被首筆方向覆蓋。
    rej = [_r("vol", "buy", "sl", -1.0), _r("vol", "buy", "tp1", 1.5),
           _r("vol", "sell", "sl", -0.8), _r("vol", "sell", "tp1", 1.2)]
    out = L._rules(rej, cand_closed=[])
    dirs = sorted(item["direction"] for item in out if item["code"] == "vol")
    assert dirs == ["buy", "sell"]                       # 兩個方向各一組
    # 每組 n 應為該方向筆數（2），不是全部 4
    for item in out:
        if item["code"] == "vol":
            assert item["n"] == 2


def test_rules_verdict_not_causal():
    # 足量且顯著為負時，措辭不得再用「規則在保護你」這種對 0 比較的因果宣稱。
    rej = [_r("vol", "buy", "sl", -1.0 if i % 2 else -1.4) for i in range(L.MIN_N + 5)]
    out = L._rules(rej, cand_closed=[])
    v = next(item for item in out if item["code"] == "vol")["verdict"]
    assert "在保護你" not in v
    assert "與 0 比較" in v or "樣本不足" in v
