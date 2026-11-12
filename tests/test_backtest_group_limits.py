"""Q-P1-08：組合回測套用與正式掃描相同的分散規則（同產業去重、相關性群組上限）。"""
import backtester as bt
import config


def test_group_key_matches_the_scanner():
    import scanner
    for sector, ticker in (("半導體業", "2330.TW"), ("電子零組件業", "2308.TW"), ("金融保險", "2881.TW"),
                           ("水泥工業", "1101.TW"), ("其他", "0050.TW"), ("", "9999.TW"), ("", ""), ("其他", "")):
        assert bt._bt_group_key(sector, ticker) == scanner._correlation_group_key(sector, ticker)


def _c(score, ticker):
    return (score, ticker, "buy", 100.0, 100.0, 95.0, {})


def test_same_sector_keeps_only_the_best_and_unknown_sectors_are_independent():
    sec = {"A.TW": "水泥工業", "B.TW": "水泥工業", "C.TW": "其他", "D.TW": "其他", "E.TW": ""}
    cands = [_c(90, "A.TW"), _c(85, "B.TW"), _c(80, "C.TW"), _c(78, "D.TW"), _c(76, "E.TW")]
    kept, n_sector, n_group = bt._bt_apply_group_limits(cands, {}, sec.get)
    assert [c[1] for c in kept] == ["A.TW", "C.TW", "D.TW", "E.TW"] and (n_sector, n_group) == (1, 0)


def test_correlated_group_cap_counts_open_positions():
    assert config.MAX_PER_CORRELATION_GROUP == 2
    sec = {"S1.TW": "半導體業", "S2.TW": "電子零組件業", "S3.TW": "電腦及週邊設備業", "F1.TW": "金融保險",
           "HELD.TW": "半導體業"}
    cands = [_c(95, "S1.TW"), _c(90, "S2.TW"), _c(85, "S3.TW"), _c(80, "F1.TW")]
    kept, n_sector, n_group = bt._bt_apply_group_limits(cands, {}, sec.get)
    assert [c[1] for c in kept] == ["S1.TW", "S2.TW", "F1.TW"] and (n_sector, n_group) == (0, 1)
    # 已持有一檔半導體：同群組只能再進一檔
    kept, _, n_group = bt._bt_apply_group_limits(cands, {"HELD.TW": {}}, sec.get)
    assert [c[1] for c in kept] == ["S1.TW", "F1.TW"] and n_group == 2
