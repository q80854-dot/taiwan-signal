"""LIVE19（第三階段審查）：資料覆蓋缺口。

重大訊息公告來源僅涵蓋上市（TWSE）；上櫃(.TWO)未涵蓋。上櫃個股空清單是「未查核(unknown)」，
不可被當成「無公告/無風險」。
"""
import app


def test_listed_is_covered():
    c = app._material_news_coverage("2330.TW")
    assert c["covered"] is True and c["market"] == "上市"


def test_otc_is_not_covered_and_flagged_unknown():
    c = app._material_news_coverage("6488.TWO")
    assert c["covered"] is False and c["market"] == "上櫃"
    assert "未查核" in c["note"] and "無風險" in c["note"]


def test_blank_ticker_defaults_listed_shape():
    c = app._material_news_coverage("")
    assert set(c) >= {"covered", "market", "note", "source"}
