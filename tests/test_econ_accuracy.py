"""財經日曆正確性／時效（2026-11-06）：公布時段短快取判斷、FRED 官方是否已納入本期。
對應使用者回報：公布當下顯示錯值（FMP 初值），過一段時間才恢復正確。"""
from datetime import datetime, timezone
import econ_calendar as ec

_UTC = timezone.utc


# ── _hot_window：美國數據密集公布時段（平日美東 08:25–10:45）──
def test_hot_window_at_830_et_weekday():
    # 2026-11-06 週四，EST(UTC-5)；08:30 ET = 13:30 UTC
    assert ec._hot_window(datetime(2026, 11, 6, 13, 30, tzinfo=_UTC)) is True


def test_hot_window_1000_et_weekday():
    assert ec._hot_window(datetime(2026, 11, 6, 15, 0, tzinfo=_UTC)) is True   # 10:00 ET


def test_not_hot_window_afternoon():
    assert ec._hot_window(datetime(2026, 11, 6, 20, 0, tzinfo=_UTC)) is False  # 15:00 ET


def test_not_hot_window_weekend():
    assert ec._hot_window(datetime(2026, 11, 7, 13, 30, tzinfo=_UTC)) is False  # 週六


# ── _fred_is_current：FRED 官方是否已納入本期 ──
def test_weekly_fred_ingested_is_current():
    # 週報：公布日 11/06，最新觀測週結束 11/01（5 天前）、次新 10/25 → 已納入本期
    assert ec._fred_is_current("2026-11-06", "2026-11-01", "2026-10-25") is True


def test_weekly_fred_not_ingested_is_stale():
    # 週報：FRED 尚未更新，最新觀測仍是上週 10/25（12 天前）→ 非本期，不可覆蓋
    assert ec._fred_is_current("2026-11-06", "2026-10-25", "2026-10-18") is False


def test_monthly_fred_ingested_is_current():
    # 月報：公布日 11/13 反映 10 月（觀測日 10/01，約 43 天前）→ 已納入本期
    assert ec._fred_is_current("2026-11-13", "2026-10-01", "2026-09-01") is True


def test_monthly_fred_not_ingested_is_stale():
    # 月報：FRED 尚未更新，最新仍是 9 月（觀測日 09/01，約 73 天前）→ 非本期
    assert ec._fred_is_current("2026-11-13", "2026-09-01", "2026-08-01") is False


def test_fred_current_rejects_missing_or_future_dates():
    assert ec._fred_is_current("2026-11-06", None, "2026-10-25") is False
    assert ec._fred_is_current("2026-11-06", "2026-11-10", "2026-11-03") is False  # 觀測日晚於公布日
