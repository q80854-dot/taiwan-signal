"""backfill_inst_daily 中間缺漏回補（2026-10-15 修正 A1）：
原本一旦累積滿 want_days 天就只補今天，中間漏掉的交易日永遠不會回補。
這裡驗證：資料庫已有很多天、但視窗內有個「洞」時，回補會去補那個洞。"""
from datetime import datetime, timezone, timedelta
import data_fetcher as df

TPE = timezone(timedelta(hours=8))


def _recent_weekdays(n):
    out, d = [], datetime.now(timezone.utc) + timedelta(hours=8)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return out


class _FakeStore:
    def __init__(self, have):
        self._have = list(have)
    def get_inst_dates(self, limit=180):
        return list(self._have)


def test_backfill_fills_interior_gap(monkeypatch):
    monkeypatch.setattr(df, "_is_tw_market_holiday", lambda now: False)
    monkeypatch.setattr(df.time, "sleep", lambda *a, **k: None)
    wd = _recent_weekdays(30)
    hole = wd[5]                       # 第 6 近的交易日故意缺一天（中間的洞）
    have = [x for x in wd if x != hole]
    attempted = []
    def _fake_collect(ymd, store):
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        attempted.append(iso)
        return 1, "ok"
    monkeypatch.setattr(df, "collect_inst_daily", _fake_collect)
    res = df.backfill_inst_daily(_FakeStore(have), want_days=30)
    assert hole in attempted, f"中間缺漏的 {hole} 應該被回補，實際嘗試：{attempted}"


def test_backfill_no_gap_only_tries_missing(monkeypatch):
    monkeypatch.setattr(df, "_is_tw_market_holiday", lambda now: False)
    monkeypatch.setattr(df.time, "sleep", lambda *a, **k: None)
    wd = _recent_weekdays(30)
    # 今天（wd[0]）故意沒有 → 應只補今天，其餘都在就不重抓
    have = wd[1:]
    attempted = []
    monkeypatch.setattr(df, "collect_inst_daily",
                        lambda ymd, store: (attempted.append(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"), (1, "ok"))[1])
    df.backfill_inst_daily(_FakeStore(have), want_days=30)
    assert attempted == [wd[0]], f"只有今天缺，應只補今天，實際：{attempted}"
