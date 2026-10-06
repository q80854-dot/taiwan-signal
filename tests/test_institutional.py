import data_fetcher


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _row(code, name, fgn_shares, trust_shares, total_shares):
    r = [""] * 19
    r[0], r[1], r[4], r[10], r[18] = code, name, f"{fgn_shares:,}", f"{trust_shares:,}", f"{total_shares:,}"
    return r


def test_t86_units_converted_from_shares_to_lots(monkeypatch):
    payload = {"stat": "OK", "data": [_row("2330", "台積電", 5_000_000, 20_000, 5_020_000),
                                      _row("1101", "台泥", -300_000, 0, -300_000)]}
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: _Resp(payload))
    res, st = data_fetcher._inst_try("20261005")
    assert st == "ok"
    assert res["2330"]["foreign_net"] == 5000          # 5,000,000 股 = 5,000 張
    assert res["2330"]["trust_net"] == 20
    assert res["1101"]["foreign_net"] == -300
    assert res["2330"]["signal"] == "strong_buy"       # 外資 >500 張且投信 >0


def test_t86_non_trading_day_is_empty(monkeypatch):
    monkeypatch.setattr(data_fetcher.requests, "get", lambda *a, **k: _Resp({"stat": "很抱歉，沒有符合條件的資料!"}))
    res, st = data_fetcher._inst_try("20261003")
    assert st == "empty" and res == {}
