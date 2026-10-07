import taifex as tf


def _fut(c, who, net, lo=0, sh=0):
    return {"Date": "20261006", "ContractCode": c, "Item": who, "OpenInterest(Net)": str(net),
            "OpenInterest(Long)": str(lo), "OpenInterest(Short)": str(sh)}


def test_parse_futures_equivalent_contracts():
    rows = [_fut("臺股期貨", "外資及陸資", "-79,517", 12968, 92485), _fut("小型臺指期貨", "外資及陸資", 4414),
            _fut("微型臺指期貨", "外資及陸資", -3144), _fut("電子期貨", "外資及陸資", 999), _fut("臺股期貨", "投信", 100)]
    r = tf.parse_futures(rows)
    f = r["items"]["外資及陸資"]
    assert r["date"] == "2026-10-06" and f["tx_net_oi"] == -79517
    assert f["equiv_net_oi"] == round(-79517 + 4414 * 0.25 - 3144 * 0.05)      # 其他契約不計入
    assert r["items"]["投信"]["equiv_net_oi"] == 100


def test_parse_options_pcr_large():
    o = tf.parse_options([{"ContractCode": "臺指選擇權", "CallPut": "CALL", "Item": "外資及陸資", "OpenInterest(Net)": "37"},
                          {"ContractCode": "臺指選擇權", "CallPut": "PUT", "Item": "外資及陸資", "OpenInterest(Net)": "1,240"},
                          {"ContractCode": "其他", "CallPut": "PUT", "Item": "外資及陸資", "OpenInterest(Net)": "5"}])
    assert o == {"外資及陸資": {"call_net_oi": 37, "put_net_oi": 1240}}
    p = tf.parse_pcr([{"Date": "20261006", "PutCallOIRatio%": "95.47", "PutCallVolumeRatio%": "107.91"},
                      {"Date": "20260907", "PutCallOIRatio%": "90", "PutCallVolumeRatio%": "100"}, {"Date": "bad"}])
    assert [x["date"] for x in p] == ["2026-09-07", "2026-10-06"] and p[-1]["oi_ratio"] == 95.47
    l = tf.parse_large([{"Contract": "TX", "SettlementMonth": "202610", "TypeOfTraders": "0", "Top10Buy": "1", "Top10Sell": "1", "Top5Buy": "1", "Top5Sell": "1", "OIOfMarket": "1"},
                        {"Contract": "TX", "SettlementMonth": "999912", "TypeOfTraders": "0", "Top5Buy": "75582", "Top5Sell": "57108",
                         "Top10Buy": "83651", "Top10Sell": "79298", "OIOfMarket": "121509"}])
    assert l["top10_net"] == 4353 and l["top10_net_pct"] == 3.6


def test_summary_and_hist(monkeypatch):
    class S:
        d = {}
        def get_meta(self, k, default=None): return self.d.get(k, default)
        def set_meta(self, k, v): self.d[k] = v
    data = {"futures": {"date": "2026-10-06", "items": {"外資及陸資": {"equiv_net_oi": -80000}}},
            "pcr": [{"date": "2026-10-06", "oi_ratio": 95.0, "vol_ratio": 1}], "errors": []}
    monkeypatch.setattr(tf, "get_taifex", lambda force=False: data)
    st = S()
    st.d["taifex_hist"] = {f"2026-09-{i:02d}": {"equiv": {"外資及陸資": -70000 - i}} for i in range(20, 27)}
    assert tf.update_hist(st, data) == "2026-10-06" and st.d["taifex_hist"]["2026-10-06"]["pcr_oi"] == 95.0
    s = tf.summary(st)
    assert s["foreign_equiv_net_oi"] == -80000 and "淨空單" in s["foreign_note"]
    assert s["foreign_chg_5d"] == -80000 - (-70000 - 22)          # 往前第 5 個交易日


def test_merge_keep_last_uses_old_when_source_fails():
    old = {"futures": {"date": "2026-10-07", "items": {"外資及陸資": {"equiv_net_oi": -100}}}, "options": {"x": 1}, "pcr": [{"date": "2026-10-07", "oi_ratio": 1}], "large": {"a": 1}}
    new = {"errors": ["futures: empty"], "options": {"y": 2}, "pcr": [], "large": None}
    m = tf.merge_keep_last(new, old)
    assert m["futures"] == old["futures"] and m["pcr"] == old["pcr"] and m["large"] == old["large"]
    assert m["options"] == {"y": 2}
    assert set(m["stale"]) == {"futures", "pcr", "large"}


def test_merge_keep_last_without_old():
    m = tf.merge_keep_last({"errors": []}, None)
    assert m["stale"] == []
