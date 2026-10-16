"""2026-10-16 根因修復：上市（或上櫃）日行情抓取失敗時，整個市場原本會從清單裡
消失（導致「查無個股」「排行榜上市全空」）。驗證單一市場抓失敗會沿用前次清單、
標記 stale，不讓一整個市場消失。"""
import time

import stock_universe as su


def _otc_row():
    return {"code": "6488", "ticker": "6488.TWO", "name": "環球晶", "market": "OTC",
            "close": 500.0, "volume_lots": 50, "is_etf": False, "quote_date": "2026-10-16"}


def _tse_row():
    return {"code": "2330", "ticker": "2330.TW", "name": "台積電", "market": "TSE",
            "close": 1000.0, "volume_lots": 100, "is_etf": False, "quote_date": "2026-10-15"}


def test_tse_fetch_fail_keeps_previous_tse_list(monkeypatch):
    prior = {"stocks": [_tse_row()], "sector_map": {"2330": "半導體業"}, "fetched_at": time.time()}
    monkeypatch.setattr(su, "_raw_mem_cache", prior, raising=False)
    monkeypatch.setattr(su, "_raw_mem_cache_at", time.time(), raising=False)
    monkeypatch.setattr(su, "_fetch_twse_list", lambda: [])            # 上市抓失敗
    monkeypatch.setattr(su, "_fetch_tpex_list", lambda: [_otc_row()])  # 上櫃正常
    monkeypatch.setattr(su, "_fetch_sector_info", lambda: {"2330": "半導體業"})
    monkeypatch.setattr(su, "_save_raw_to_db", lambda payload: None)
    alerts = []
    monkeypatch.setattr(su, "_alert_market_fetch_fail", lambda m, n: alerts.append((m, n)))

    raw = su._get_raw_universe(force_refresh=True)
    codes = {s["code"] for s in raw["stocks"]}
    assert "2330" in codes, "上市 2330 應被前次清單保留，不應消失"
    assert "6488" in codes, "上櫃新鮮資料應保留"
    assert raw["tse_stale"] is True and raw["otc_stale"] is False
    assert alerts and alerts[0][0] == "上市"


def test_both_markets_present_not_stale(monkeypatch):
    monkeypatch.setattr(su, "_raw_mem_cache", None, raising=False)
    monkeypatch.setattr(su, "_fetch_twse_list", lambda: [_tse_row()])
    monkeypatch.setattr(su, "_fetch_tpex_list", lambda: [_otc_row()])
    monkeypatch.setattr(su, "_fetch_sector_info", lambda: {"2330": "半導體業"})
    monkeypatch.setattr(su, "_save_raw_to_db", lambda payload: None)
    raw = su._get_raw_universe(force_refresh=True)
    assert raw["tse_stale"] is False and raw["otc_stale"] is False
    assert {"2330", "6488"} <= {s["code"] for s in raw["stocks"]}
