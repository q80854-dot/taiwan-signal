"""LIVE13 計分面：異常殖利率不得加分（2026-11-12 使用者核准）。

殖利率 > config.YIELD_ANOMALY_PCT（15%）多為資料問題（除權息基準/面額變更/拆分/
幣別/單位），未通過核對不得進正式訊號評分（fund_adjustment）——計分視同無殖利率
（不加分、不入滿分），原值保留於回傳與標籤（血緣、不掩蓋）。
本變更屬 Champion 行為變更：YIELD_ANOMALY_PCT 入 strategy_version._TRACKED、
CHAMPION_SNAPSHOT.json 同步重產（champion-02595bc0c128）。
"""
from fund_score import _valuation


def test_anomalous_yield_not_scored():
    v = _valuation({"pe": 15, "pb": 1.2, "yield_pct": 148.45})   # 審查實例：世紀* 148.45%
    assert v["yield_anomaly"] is True
    assert v["yield"] == 148.45                                   # 原值保留（血緣）
    assert v["max"] == 15                                         # 滿分不含殖利率 5 分（12+3）
    assert any("異常待核對" in t for t in v["tags"])              # 誠實標示、不掩蓋

    v2 = _valuation({"pe": 15, "pb": 1.2, "yield_pct": 5.0})      # 對照：正常殖利率照常加分
    assert v2["yield_anomaly"] is False and v2["max"] == 20
    assert v2["got"] - v["got"] == 5


def test_boundary_not_anomalous():
    v = _valuation({"pe": 15, "pb": 1.2, "yield_pct": 15.0})      # 門檻值本身（嚴格 >）不算異常
    assert v["yield_anomaly"] is False and v["max"] == 20


def test_yield_anomaly_is_champion_tracked():
    import strategy_version as sv
    assert "YIELD_ANOMALY_PCT" in sv._TRACKED                     # 變更控管：改門檻＝改 Champion
