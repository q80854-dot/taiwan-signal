"""Shadow 雙軌：Champion Track（分批出場淨 R）（2026-11-09，依第二階段審查 §四-1）。

審查點名：shadow 用單次出場、正式策略用 TP1/TP2/TP3 各 1/3 分批，兩者淨 R 不同，污染因子研究。
本測試驗證新增的 _champion_r（與正式一致的分批出場淨 R），與既有 r_multiple（單次=Research）並存。
"""
import shadow


def _bar(d, o, h, l, c):
    return {"bar_date": f"2026-01-{d:02d}", "open": o, "high": h, "low": l, "close": c}


_ROW = {"bar_date": "2026-01-01", "direction": "buy",
        "entry": 100.0, "stop": 95.0, "tp1": 103.0, "tp2": 106.0, "tp3": 110.0}


def test_champion_rides_further_than_partial_exit():
    # 情境 A：分批全程獲利（day2 TP1、day3 TP2、day4 TP3）→ champion_r 明顯為正。
    bars_a = [_bar(2, 100, 103, 99, 102), _bar(3, 102, 106, 102, 105), _bar(4, 105, 110, 105, 109)]
    champ_a = shadow._champion_r(_ROW, bars_a, expire_days=30)
    # 情境 B：day2 TP1 後 1/3 出場、停損移保本，day3 跌回保本把其餘 2/3 停損 → champion_r 較小。
    bars_b = [_bar(2, 100, 103, 99, 102), _bar(3, 101, 102, 99, 100)]
    champ_b = shadow._champion_r(_ROW, bars_b, expire_days=30)
    assert champ_a is not None and champ_b is not None
    assert champ_a > champ_b                 # 全程分批 > 只吃到 TP1 後保本出場
    assert champ_a > 0                        # 三段皆獲利，淨 R 為正


def test_champion_respects_expire_window():
    # TP2/TP3 落在到期窗之外 → champion_r 不得把到期後的停利計入（應低於不設窗）。
    bars = [_bar(2, 100, 103, 99, 102)]                       # day2 TP1
    bars += [{"bar_date": f"2026-01-{d:02d}", "open": 102, "high": 104, "low": 101, "close": 103}
             for d in range(3, 18)]                            # 盤整、未再觸及
    bars.append(_bar(25, 103, 112, 103, 111))                  # day25（到期後）才噴 TP3
    champ_windowed = shadow._champion_r(_ROW, bars, expire_days=15)
    champ_open = shadow._champion_r(_ROW, bars, expire_days=999)
    assert champ_windowed is not None and champ_open is not None
    assert champ_open > champ_windowed        # 不設窗會多吃到期後的 TP3，較高（確認窗有效）


def test_champion_r_none_on_degenerate_risk():
    bad = dict(_ROW, entry=100.0, stop=100.0)   # entry≈stop，R 無意義
    assert shadow._champion_r(bad, [_bar(2, 100, 103, 99, 102)], expire_days=30) is None


def test_champion_r_none_when_no_future_bars():
    assert shadow._champion_r(_ROW, [], expire_days=30) is None
