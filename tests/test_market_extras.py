import market_extras as mx
from datetime import datetime


def test_roc_and_num():
    assert mx._roc("1150813") == "2026-08-13" and mx._roc("") is None and mx._roc("abc") is None
    assert mx._num("13,347,514") == 13347514 and mx._num("--") is None


def test_parse_twt93u_sample_row():
    row = ["2330", "台積電", "46,000", "7,000", "4,000", "0", "49,000", "6,483,092,516",
           "14,202,514", "306,000", "1,161,000", "0", "13,347,514", "6,504,427", " "]
    r = mx.parse_twt93u({"stat": "OK", "data": [row, ["x"]]})
    assert r == {"2330": [49000, 13347514, 46000, 14202514]}
    assert mx.parse_twt93u({"stat": "no"}) == {}


def test_build_flags(monkeypatch):
    data = {
        "TWTAWU": [{"Code": "1218", "TradingHaltDate": "1151007", "TradingHaltTime": "080000",
                    "TradingResumptionDate": "1151009", "TradingResumptionTime": "080000"},
                   {"Code": "1111", "TradingHaltDate": "1150813", "TradingHaltTime": "080000",
                    "TradingResumptionDate": "1150814", "TradingResumptionTime": "080000"}],
        "TWT85U": [{"Code": "1213"}],
        "BFI84U": [{"Code": "00400A", "StartDate": "1151002", "EndDate": "1151007", "Reason": "分配收益"},
                   {"Code": "2222", "StartDate": "1150101", "EndDate": "1150102", "Reason": "x"}],
        "TWT88U": [{"SecurCode": "7689", "5thTradingDate": "1151010"}, {"SecurCode": "7000", "5thTradingDate": "1150101"}],
        "TWTB4U": [{"Code": "2330", "Suspension": ""}, {"Code": "2317", "Suspension": "Y"}],
        "TWT96U": [{"TWSECode": "2330", "TWSEAvailableVolume": "1,000,000", "GRETAICode": "6488", "GRETAIAvailableVolume": "5,000"}],
    }
    monkeypatch.setattr(mx, "_get", lambda url, timeout=20: next(v for k, v in data.items() if url.endswith(k)))
    f = mx.build_flags(datetime(2026, 10, 7, 12, 0))
    assert list(f["halt"]) == ["1218"] and f["altered"] == ["1213"]
    assert list(f["margin_stop"]) == ["00400A"] and f["no_limit"] == ["7689"]
    assert f["daytrade_ok"] == ["2317", "2330"] and f["daytrade_suspended"] == ["2317"]
    assert f["sbl_avail"] == {"2330": 1000000, "6488": 5000} and not f["errors"]


def test_failed_source_isolated_and_hard_exclude(monkeypatch):
    def fake(url, timeout=20):
        if url.endswith("TWT85U"):
            raise RuntimeError("boom")
        return []
    monkeypatch.setattr(mx, "_get", fake)
    f = mx.build_flags(datetime(2026, 10, 7, 12, 0))
    assert len(f["errors"]) == 1 and "變更交易" in f["errors"][0]
    monkeypatch.setattr(mx, "get_flags", lambda force=False: {"halt": {"1": {}}, "altered": ["2"]})
    assert mx.hard_exclude_codes() == {"1", "2"}


def test_short_summary():
    h = {f"2026-10-0{i}": {"2330": [1000 * i, 100000 * i, 0, 0]} for i in range(1, 8)}
    s = mx.short_summary(h, "2330", volume_lots=1000)
    assert s["date"] == "2026-10-07" and s["margin_short_lots"] == 7.0 and s["sbl_short_lots"] == 700.0
    assert s["chg_days"] == 5 and s["sbl_short_chg_lots"] == 500.0   # 第 7 天 vs 第 2 天
    assert s["short_vs_volume_days"] == 0.71 and mx.short_summary(h, "9999") is None
