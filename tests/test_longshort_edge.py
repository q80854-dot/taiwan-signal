"""TS-SHORT-001 修正：edge「訊號 vs 隨機」比較須方向配對（只拿做多候選對隨機做多對照）。

對照組是隨機做多(long-only)，若候選含放空就比較，會方向組成混淆。本測試故意放一批很差的
放空候選，驗證它們不會污染「做多 vs 隨機做多」這條結論。
"""
import learning as L


def _mk(kind, direction, r, day, i, result):
    return {"kind": kind, "status": "closed", "direction": direction, "r_multiple": r,
            "bar_date": f"2026-10-{day:02d}", "ticker": f"{kind[:2]}{direction[:1]}{day}{i}",
            "code": f"{day}{i}", "sent": True, "result": result, "f": {}}


def _patched_rows():
    rows = []
    for d in range(1, 13):          # 12 個交易日
        for i in range(4):
            rows.append(_mk("candidate", "buy", 1.0, d, i, "tp1"))     # 做多候選：好
            rows.append(_mk("candidate", "sell", -3.0, d, i, "sl"))    # 放空候選：很差（不該污染做多結論）
            rows.append(_mk("control", "buy", 0.0, d, i, "expired"))   # 隨機做多對照：中性
    return rows


def test_edge_is_direction_matched(monkeypatch):
    monkeypatch.setattr(L, "_rows", _patched_rows)
    out = L.build_report()
    edge = out["edge"]
    # 比較限定做多
    assert edge.get("direction") == "buy"
    # 做多候選(平均 R≈1.0) 顯著優於 隨機做多對照(0.0)——放空的 -3.0 沒有把結論拉成「劣於」
    assert edge["significant"] is True
    assert edge["diff_r"] > 0
    assert "做多訊號" in edge["text"]


def test_mixed_short_does_not_flip_edge(monkeypatch):
    """即使放空候選極差，edge 仍反映做多的真實優勢（證明混合已被排除）。"""
    monkeypatch.setattr(L, "_rows", _patched_rows)
    out = L.build_report()
    # 若誤用混合候選，平均 R 會被 -3.0 的放空拖成負值、結論反轉；這裡必為正向顯著
    assert out["edge"]["diff_r"] > 0.5
