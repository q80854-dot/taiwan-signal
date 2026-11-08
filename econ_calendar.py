"""美國財經日曆（首頁用）。

使用者要求首頁有財經日曆，涵蓋美國所有重要經濟數據：升／降息（FOMC）、大小非農（NFP/ADP）、
初領失業救濟金、CPI、PCE、PPI、零售、GDP、ISM 等，並像富途顯示「前值／預測值／公佈值」。

資料來源採「兩者結合」（使用者選擇）：
  1. 主來源 FMP economic_calendar（需 FMP_API_KEY）：提供事件、前值/預測值/公佈值/重要度。
     FMP 官方 FAQ 明載事件時間為 UTC，這裡統一換算成台北時間（+8）顯示並標註。
  2. 備援/校驗 FRED releases/dates（需 FRED_API_KEY）：美國聖路易聯準會官方「發布排程」。
     只在「真正排程公布」那天列出事件——用 FRED 穩定的 release_id 白名單精準比對（不用寬鬆關鍵字，
     以免每個營業日都更新的資料如「Federal Funds Data」被誤判成 FOMC 天天出現）；白名單由已查證的
     靜態項 ＋ 向 FRED 問出各關注數列所屬 release 組成。FOMC 利率決議非 FRED 資料發布，改用 Fed
     官方行事曆寫死、只在決議日出現。
  另：FMP 經濟日曆為付費端點（免費方案回 402），故免費情境下再用 FRED series/observations
  免費補「前值／公佈值」（僅對照明確、FRED 自帶換算＝市場口徑的指標；「預測值」無免費來源留 —）。

守住「不要資訊錯誤」：沒設金鑰就回 status="no_source"、events 空（前端顯示「尚未設定資料來源」），
絕不編造；抓取/解析失敗一律略過該筆、不塞假值；數值缺就留 None（顯示 —）。
整體記憶體快取 1 小時，另由排程預熱，首頁讀到的是快取。
"""
import os
import time
import logging
from datetime import datetime, timedelta, timezone

import pytz
import requests

logger = logging.getLogger(__name__)
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TPE = timezone(timedelta(hours=8))
_UTC = timezone.utc
# ★ 2026-11-04：財經日曆改以「實際公佈時刻」為準，而非整日。美國官方數據的固定發布時間以
# 「美東時間（ET）」排定，而美東會在夏令（EDT=UTC-4，約 3 月第二個週日～11 月第一個週日）與
# 冬令（EST=UTC-5）之間切換。用 pytz 的 America/New_York 對「該發布日的 ET 時刻」做 localize，
# 夏令/冬令會自動判斷，轉成 UTC 與台北時間都正確（倒數也倒數到這個時刻）。
_ET = pytz.timezone("America/New_York")

# 指標中文名 → 美東固定發布時刻 (時, 分)。多數 BLS/BEA/Census 數據為 08:30 ET；
# 部分調查/報告為 10:00 ET；ADP 08:15、工業生產 09:15、FOMC 會議紀要 14:00。
# 來源：各發布機構官方行事曆（BLS、BEA、Census、ISM、Conference Board、Fed）。
# 沒列到的（如 Fed 官員談話，時間不固定）維持無時刻＝顯示「全日」，不編造。
_RELEASE_TIME_ET = {
    "非農就業人數（NFP）": (8, 30),
    "初領失業救濟金人數": (8, 30),
    "消費者物價指數（CPI）": (8, 30),
    "生產者物價指數（PPI）": (8, 30),
    "PCE 物價指數（Fed 最看重）": (8, 30),
    "零售銷售": (8, 30),
    "GDP 國內生產毛額": (8, 30),
    "失業率": (8, 30),
    "平均時薪": (8, 30),
    "耐用品訂單": (8, 30),
    "貿易收支": (8, 30),
    "新屋開工": (8, 30),
    "營建許可": (8, 30),
    "ADP 民間就業人數（小非農）": (8, 15),
    "工業生產": (9, 15),
    "ISM 製造業 PMI": (10, 0),
    "ISM 服務業 PMI": (10, 0),
    "JOLTS 職位空缺": (10, 0),
    "密西根大學消費者信心": (10, 0),
    "消費者信心指數": (10, 0),
    "新屋銷售": (10, 0),
    "成屋銷售": (10, 0),
    "工廠訂單": (10, 0),
    "FOMC 會議紀要": (14, 0),
}


def _et_release(date_str: str, hh: int, mm: int):
    """把「美國發布日 date_str（YYYY-MM-DD）的 ET hh:mm」換成 (UTC datetime, 台北 datetime)。
    pytz 會依該日期自動套用夏令(EDT)或冬令(EST)，不需手動判斷。失敗回 None。"""
    try:
        y, m, d = (int(x) for x in date_str[:10].split("-"))
        et = _ET.localize(datetime(y, m, d, hh, mm))
        return et.astimezone(_UTC), et.astimezone(_TPE)
    except Exception as e:
        logger.warning(f"econ_calendar _et_release({date_str},{hh}:{mm}): {e}")
        return None
_cache = {}
_TTL = 3600
_FAIL_TTL = 120

# FMP 已於 2024–2025 將 API 遷移到 /stable；新申請的金鑰打舊 /api/v3 會回 403「Legacy Endpoint」，
# 故採官方現行端點。參數（from/to/apikey）與回傳欄位（date/country/event/previous/estimate/actual/impact）
# 與舊版相同，解析不受影響。
FMP_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FRED_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_RELEASE_URL = "https://api.stlouisfed.org/fred/series/release"

# 只列「真正排程公布」的官方發布，避免把 FRED 每個營業日都更新的資料（如「Federal Funds Data」）
# 用關鍵字誤判成排程事件（例如天天冒出 FOMC）。改用 FRED 官方穩定的 release_id 白名單精準比對。
# 這三個 release_id 已查證（FRED/ALFRED）；其餘（初領失業金、PCE、零售…）在執行期向 FRED
# 的 series/release 問出所屬 release_id（權威、零猜測），見 fetch 內 _build_release_whitelist。
# release_id → (中文名, 重要度)。中文名刻意對齊 _FRED_SERIES 的鍵，讓前值/公佈值能接著補上。
_FRED_RELEASE_WL = {
    50: ("非農就業人數（NFP）", "高"),        # Employment Situation（就業報告，頭條＝非農）
    10: ("消費者物價指數（CPI）", "高"),       # Consumer Price Index
    46: ("生產者物價指數（PPI）", "高"),       # Producer Price Index
}

# FOMC 利率決議不是 FRED 的「資料發布」，無法從 releases/dates 可靠取得；改用 Fed 官方 FOMC
# 行事曆寫死（只在會議第 2 天＝週三 美東 14:00 公布決議）。存成 UTC（2026 年 3/8–11/1 為 EDT=UTC-4，
# 其餘為 EST=UTC-5），再與其他事件一樣換算台北時間顯示，只在決議當天出現、不會天天寫。
# 來源：federalreserve.gov FOMC calendar。日後每年更新一次即可。
_FOMC_DECISIONS_UTC = [
    "2026-01-28T19:00:00+00:00",  # EST
    "2026-03-18T18:00:00+00:00",  # EDT
    "2026-04-29T18:00:00+00:00",
    "2026-06-17T18:00:00+00:00",
    "2026-07-29T18:00:00+00:00",
    "2026-09-16T18:00:00+00:00",
    "2026-10-28T18:00:00+00:00",
    "2026-12-09T19:00:00+00:00",  # EST（11/1 後）
]

# 免費補「前值／公佈值」用：指標中文名 → (FRED 數列代號, 單位換算, 單位標示)。
# 只收「FRED 自帶換算後＝市場公佈口徑」且對照明確者，避免資訊錯誤：
#   PAYEMS chg＝非農當月增減（千人，＝市場頭條口徑）、ICSA 為初領失業金人數、
#   CPIAUCSL/PCEPI 的 pc1＝年增率(%)、RSAFS 的 pch＝零售月增率(%)。
# 「預測值」（市場共識）無免費來源，一律留 None（顯示 —）。其餘指標（ADP、ISM、FOMC…）
# 免費亦無可靠公佈值，維持無數值。FRED 回傳為最新修正值（非初值），來源標 FRED。
_FRED_SERIES = {
    "非農就業人數（NFP）": ("PAYEMS", "chg", "千人"),
    "初領失業救濟金人數": ("ICSA", "lin", "人"),
    "消費者物價指數（CPI）": ("CPIAUCSL", "pc1", "%（年增）"),
    "PCE 物價指數（Fed 最看重）": ("PCEPI", "pc1", "%（年增）"),
    "零售銷售": ("RSAFS", "pch", "%（月增）"),
}


def _fmp_key() -> str:
    return (os.environ.get("FMP_API_KEY") or "").strip()


def _fred_key() -> str:
    return (os.environ.get("FRED_API_KEY") or "").strip()


# 指標中文對照＋重要度（高/中/低）。關鍵字（小寫）比對 FMP 事件名或 FRED 發布名皆適用。
# 「其他沒講到的」一併納入（PCE、PPI、GDP、ISM、零售、JOLTS、消費者信心、Fed 官員談話…）。
_INDICATORS = [
    (["nonfarm payroll", "non-farm payroll", "nonfarm", "employment situation", "change in nonfarm"], "非農就業人數（NFP）", "高"),
    (["adp employment", "adp nonfarm", "adp "], "ADP 民間就業人數（小非農）", "高"),
    (["initial jobless", "initial claims", "continuing jobless", "continuing claims", "unemployment insurance weekly", "jobless claims"], "初領失業救濟金人數", "高"),
    (["cpi", "consumer price index"], "消費者物價指數（CPI）", "高"),
    (["core pce", "pce price", "personal consumption expenditures price", "personal income and outlays"], "PCE 物價指數（Fed 最看重）", "高"),
    (["ppi", "producer price index"], "生產者物價指數（PPI）", "高"),
    (["fomc economic projections", "fomc press conference", "fomc statement"], "FOMC 聲明／記者會", "高"),
    (["fomc minutes", "fomc meeting minutes"], "FOMC 會議紀要", "中"),
    (["fomc", "federal funds", "interest rate decision", "fed interest rate", "fed funds target"], "FOMC 利率決議（升／降息）", "高"),
    (["gross domestic product", "gdp"], "GDP 國內生產毛額", "高"),
    (["retail sales"], "零售銷售", "高"),
    (["ism manufacturing", "manufacturing pmi"], "ISM 製造業 PMI", "高"),
    (["ism non-manufacturing", "ism services", "services pmi", "non-manufacturing"], "ISM 服務業 PMI", "高"),
    (["unemployment rate"], "失業率", "高"),
    (["average hourly earnings"], "平均時薪", "中"),
    (["durable goods"], "耐用品訂單", "中"),
    (["michigan"], "密西根大學消費者信心", "中"),
    (["jolts", "job openings"], "JOLTS 職位空缺", "中"),
    (["consumer confidence", "cb consumer"], "消費者信心指數", "中"),
    (["housing starts"], "新屋開工", "低"),
    (["building permits"], "營建許可", "低"),
    (["existing home sales"], "成屋銷售", "低"),
    (["new home sales"], "新屋銷售", "低"),
    (["trade balance"], "貿易收支", "低"),
    (["industrial production"], "工業生產", "低"),
    (["factory orders"], "工廠訂單", "低"),
    (["powell", "fed chair", "fed speech", "speaks", "testimony", "fed's "], "Fed 官員談話", "中"),
]
_IMPACT_ZH = {"high": "高", "medium": "中", "low": "低", "holiday": "假日"}


def _match(name: str):
    low = (name or "").lower()
    for kws, zh, imp in _INDICATORS:
        if any(k in low for k in kws):
            return zh, imp
    return None, None


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None


def _fmp_events(frm: str, to: str):
    """回傳 FMP 美國事件原始列表；金鑰未設或失敗回 None（區分「沒來源」與「來源回空」）。"""
    key = _fmp_key()
    if not key:
        return None
    try:
        r = requests.get(FMP_URL, params={"from": frm, "to": to, "apikey": key}, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            logger.warning(f"econ_calendar FMP HTTP {r.status_code}")
            return None
        rows = r.json()
        if not isinstance(rows, list):
            logger.warning(f"econ_calendar FMP 非預期回應：{str(rows)[:120]}")
            return None
        return rows
    except Exception as e:
        logger.warning(f"econ_calendar FMP: {e}")
        return None


def _fred_future_releases(frm: str, to: str):
    """FRED 官方發布排程，回傳 [(date_str, release_id, release_name)]；未設金鑰或失敗回 []。"""
    key = _fred_key()
    if not key:
        return []
    try:
        r = requests.get(FRED_DATES_URL, params={
            "api_key": key, "file_type": "json", "sort_order": "asc",
            "include_release_dates_with_no_data": "true",
            "realtime_start": frm, "realtime_end": to, "limit": 1000,
        }, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            logger.warning(f"econ_calendar FRED HTTP {r.status_code}")
            return []
        out = []
        for d in (r.json().get("release_dates") or []):
            ds, rid, nm = d.get("date"), d.get("release_id"), d.get("release_name") or ""
            if ds and frm <= ds <= to:
                out.append((ds, rid, nm))
        return out
    except Exception as e:
        logger.warning(f"econ_calendar FRED: {e}")
        return []


def _fred_series_release(series_id: str):
    """問 FRED「某數列屬於哪個 release」，回傳 release_id(int) 或 None（權威、零猜測）。"""
    key = _fred_key()
    if not key:
        return None
    try:
        r = requests.get(FRED_SERIES_RELEASE_URL, params={
            "series_id": series_id, "api_key": key, "file_type": "json",
        }, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            logger.warning(f"econ_calendar FRED series/release {series_id} HTTP {r.status_code}")
            return None
        rels = r.json().get("releases") or []
        return rels[0].get("id") if rels else None
    except Exception as e:
        logger.warning(f"econ_calendar FRED series/release {series_id}: {e}")
        return None


def _fred_obs_latest(series_id: str, units: str = "lin", n: int = 4):
    """FRED series/observations 最新 n 筆（新→舊），回傳 [(date, value)]；缺值(".")略過。
    units 交給 FRED 換算（chg/pc1/pch…），確保數字＝市場公佈口徑，不由本程式自算。"""
    key = _fred_key()
    if not key:
        return []
    try:
        r = requests.get(FRED_OBS_URL, params={
            "series_id": series_id, "api_key": key, "file_type": "json",
            "units": units, "sort_order": "desc", "limit": n,
        }, headers=_HEADERS, timeout=12)
        if r.status_code != 200:
            logger.warning(f"econ_calendar FRED obs {series_id} HTTP {r.status_code}")
            return []
        out = []
        for o in (r.json().get("observations") or []):
            v = _num(o.get("value"))
            if v is not None:
                out.append((o.get("date"), v))
        return out
    except Exception as e:
        logger.warning(f"econ_calendar FRED obs {series_id}: {e}")
        return []


def _hot_window(now_utc=None) -> bool:
    """★ 2026-11-06：是否處於美國數據密集公布時段（平日美東 08:25–10:45，涵蓋 08:30 的
    初領失業金/CPI/NFP 與 10:00 的 ISM 等）。此時段快取大幅縮短，讓公布當下的錯值/初值能在
    1–2 分鐘內被校正，而不是被 1 小時快取鎖住。"""
    try:
        et = (now_utc or datetime.now(_UTC)).astimezone(pytz.timezone("America/New_York"))
        if et.weekday() >= 5:
            return False
        mins = et.hour * 60 + et.minute
        return 8 * 60 + 25 <= mins <= 10 * 60 + 45
    except Exception:
        return False


def _fred_is_current(release_date: str, obs0_date: str, obs1_date: str) -> bool:
    """FRED 官方庫是否已納入「本期」資料（用來判斷能否以 FRED 覆蓋 FMP 的公佈值）。
    作法與頻率無關：以最近兩筆觀測的日期間隔估計資料頻率（週報≈7天、月報≈30天），若最新觀測
    距公布日在「約 1.5 個週期」內，視為已納入本期；過舊則代表 FRED 尚未更新（最新值其實是前值），
    此時不可拿來當本期公佈值。缺日期一律回 False（保守）。"""
    try:
        rd = datetime.strptime(release_date[:10], "%Y-%m-%d").date()
        o0 = datetime.strptime(obs0_date[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    gap = (rd - o0).days
    if gap < 0:
        return False                                  # 觀測日竟晚於公布日 → 不合理，保守不採
    period = 7
    try:
        if obs1_date:
            o1 = datetime.strptime(obs1_date[:10], "%Y-%m-%d").date()
            period = max(1, (o0 - o1).days)
    except Exception:
        period = 7
    return gap <= period * 1.6                         # 約 1.6 個週期內＝本期；更舊代表 FRED 尚未更新


def fetch_economic_calendar(days_back: int = 1, days_ahead: int = 14, force: bool = False) -> dict:
    """彙整未來美國財經事件。回傳 {events, status, sources, window, at}。
    events 每筆：{dt(UTC iso), date, time, weekday, event, zh, importance(高/中/低),
    prev, estimate, actual, unit, source}。prev/actual 可由 FRED 官方資料庫免費補上（帶 unit 單位），
    estimate（市場共識）無免費來源恆為 None。"""
    if not force:
        c = _cache.get("econ")
        # 成功結果快取 1 小時；但在美國數據公布密集時段（_hot_window）縮短為 90 秒，讓公布當下
        # 的錯值/初值能在 1–2 分鐘內被校正，而非被 1 小時快取鎖住（過去「過一段時間才恢復正確」之因）。
        # 來源全部失敗（no_source）只快取 2 分鐘，避免一次逾時就整小時沒資料。
        ok_ttl = 90 if _hot_window() else _TTL
        if c and time.time() - c[0] < (ok_ttl if c[1].get("status") == "ok" else _FAIL_TTL):
            return c[1]

    now_utc = datetime.now(_UTC)
    frm = (now_utc - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to = (now_utc + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    sources = {"fmp": False, "fred": False}
    events = []
    seen = set()

    # 1) 主來源 FMP（含前值/預測值/公佈值/重要度）
    fmp_rows = _fmp_events(frm, to)
    if fmp_rows is not None:
        sources["fmp"] = True
        for row in fmp_rows:
            try:
                country = str(row.get("country") or "").upper()
                if country not in ("US", "USA", "UNITED STATES"):
                    continue
                raw = str(row.get("date") or "")          # FMP：UTC 時間 "YYYY-MM-DD HH:MM:SS"
                try:
                    d_utc = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC)
                except Exception:
                    try:
                        d_utc = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=_UTC)
                    except Exception:
                        continue
                d_tpe = d_utc.astimezone(_TPE)
                ev = str(row.get("event") or "").strip()
                zh, imp_m = _match(ev)
                imp = _IMPACT_ZH.get(str(row.get("impact") or "").lower()) or imp_m or "低"
                key = (d_tpe.strftime("%Y-%m-%d"), (zh or ev).lower())
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "dt": d_utc.isoformat(),
                    "date": d_tpe.strftime("%Y-%m-%d"),
                    "time": d_tpe.strftime("%H:%M"),
                    "weekday": "一二三四五六日"[d_tpe.weekday()],
                    "event": ev, "zh": zh or ev, "importance": imp,
                    "prev": _num(row.get("previous")),
                    "estimate": _num(row.get("estimate")),
                    "actual": _num(row.get("actual")),
                    "unit": "",
                    "source": "FMP",
                })
            except Exception as e:
                logger.warning(f"econ_calendar 解析 FMP 筆失敗：{e}")
                continue

    # 2) FRED 官方發布排程：只在「真正排程公布」的那天列出。用 release_id 白名單精準比對，
    #    不再用寬鬆關鍵字（避免每日資料如 Federal Funds Data 被誤判成 FOMC 天天出現）。
    #    白名單＝已查證的靜態三項 ＋ 向 FRED 問出 _FRED_SERIES 各數列所屬 release（權威）。
    fred_rows = _fred_future_releases(frm, to)
    if fred_rows:
        sources["fred"] = True
        wl = dict(_FRED_RELEASE_WL)                      # release_id -> (zh, importance)
        for zh, (sid, _u, _ul) in _FRED_SERIES.items():
            rid = _fred_series_release(sid)
            if rid and rid not in wl:
                wl[rid] = (zh, "高")
        for ds, rid, nm in fred_rows:
            spec = wl.get(rid)
            if not spec:                                 # 不在白名單（含每日資料）→ 不列
                continue
            zh, imp = spec
            key = (ds, zh.lower())
            if key in seen:
                continue
            seen.add(key)
            try:
                wd = "一二三四五六日"[datetime.strptime(ds, "%Y-%m-%d").weekday()]
            except Exception:
                wd = ""
            events.append({
                "dt": ds, "date": ds, "time": "", "weekday": wd,
                "event": nm, "zh": zh, "importance": imp,
                "prev": None, "estimate": None, "actual": None, "unit": "",
                "source": "FRED",
            })

    # 2b) FOMC 利率決議：Fed 官方行事曆寫死，只在決議日（會議第 2 天）出現，不天天寫。
    #     僅在已有實際資料來源時加入（避免 no_source / 來源失效時冒出孤單的 FOMC）。
    for iso in (_FOMC_DECISIONS_UTC if (sources["fmp"] or sources["fred"]) else []):
        try:
            d_utc = datetime.fromisoformat(iso)
            d_tpe = d_utc.astimezone(_TPE)
            ds = d_tpe.strftime("%Y-%m-%d")
            if not (frm <= ds <= to):
                continue
            zh = "FOMC 利率決議（升／降息）"
            key = (ds, zh.lower())
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "dt": d_utc.isoformat(), "date": ds, "time": d_tpe.strftime("%H:%M"),
                "weekday": "一二三四五六日"[d_tpe.weekday()],
                "event": "FOMC Interest Rate Decision", "zh": zh, "importance": "高",
                "prev": None, "estimate": None, "actual": None, "unit": "",
                "source": "Fed",
            })
        except Exception as e:
            logger.warning(f"econ_calendar FOMC 排程解析失敗：{e}")
            continue

    # 2c) 補「實際公佈時刻」：凡尚無時間（主要是 FRED 來源只給日期）且有已知美東發布時刻者，
    #     以該發布日的 ET 時刻換算 UTC／台北時間（夏令冬令由 pytz 自動判斷），讓畫面顯示實際
    #     公佈時間、倒數也倒數到該時刻；沒有已知時刻者維持「全日」，不編造。
    for e in events:
        if not e.get("time") and e.get("zh") in _RELEASE_TIME_ET:
            hh, mm = _RELEASE_TIME_ET[e["zh"]]
            got = _et_release(e["date"], hh, mm)
            if got:
                d_utc, d_tpe = got
                e["dt"] = d_utc.isoformat()
                e["date"] = d_tpe.strftime("%Y-%m-%d")
                e["time"] = d_tpe.strftime("%H:%M")
                e["weekday"] = "一二三四五六日"[d_tpe.weekday()]
                e["time_et"] = f"{hh:02d}:{mm:02d} ET"
        elif e.get("source") == "Fed" and e.get("time"):
            e["time_et"] = "14:00 ET"      # FOMC 決議：美東 14:00（已換算台北，另標美東時間）

    events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))

    # 3) 免費補「前值／公佈值」：對照明確的指標用 FRED 官方資料庫（series/observations），
    #    單位換算由 FRED 負責＝市場公佈口徑。只補 FMP 沒給數值者；「預測值」免費拿不到留 None。
    #    已排序遞增 → 每個數列只補「最近一筆未來事件」（前值＝最後公佈、公佈值待出），
    #    更遠期不亂套舊值；已到期事件則最新＝公佈值、次新＝前值。
    if _fred_key():
        today = datetime.now(_TPE).strftime("%Y-%m-%d")
        obs_cache, future_taken = {}, set()
        for e in events:
            spec = _FRED_SERIES.get(e["zh"])
            if not spec:
                continue
            is_released = e["date"] <= today
            # 未來事件：只在缺值時補前值（維持原行為）；已公佈事件一律進入下方 FRED 官方對帳
            if not is_released and (e["prev"] is not None or e["actual"] is not None):
                continue
            series_id, units, unit = spec
            if series_id not in obs_cache:
                obs_cache[series_id] = _fred_obs_latest(series_id, units)
            obs = obs_cache[series_id]
            if not obs:
                continue
            if is_released:                              # 已公佈：FRED 官方為準
                o0d = obs[0][0]
                o1v = obs[1][1] if len(obs) > 1 else None
                o1d = obs[1][0] if len(obs) > 1 else None
                if e["actual"] is None:
                    # FMP 沒給公佈值 → 直接用 FRED（沿用原行為）
                    e["actual"] = obs[0][1]; e["prev"] = o1v; e["unit"] = unit; e["source"] = "FRED"
                elif _fred_is_current(e["date"], o0d, o1d):
                    # ★ 2026-11-06：FMP 有值、且 FRED 官方已納入本期 → 以官方覆蓋（校正 FMP 可能的初值/錯值）。
                    # 若 FRED 尚未納入本期（_fred_is_current=False）→ 保留 FMP 當下值，待短 TTL 重抓再校正，
                    # 避免把 FRED 的「前值」誤當本期公佈值。
                    e["actual"] = obs[0][1]; e["prev"] = o1v; e["unit"] = unit; e["source"] = "FRED（官方校正）"
            elif series_id not in future_taken:           # 最近一筆未來事件：前值＝最後公佈
                e["prev"] = obs[0][1]
                e["unit"] = unit
                e["source"] = "FRED"
                future_taken.add(series_id)

    status = "ok" if (sources["fmp"] or sources["fred"]) else "no_source"
    out = {"events": events, "status": status, "sources": sources,
           "window": {"from": frm, "to": to}, "tz": "Asia/Taipei", "at": time.time()}
    if status != "ok" and (_fmp_key() or _fred_key()):
        good = _cache.get("econ_good")
        if not good:
            try:                                        # 重啟後記憶體清空 → 讀資料庫存的上一份成功資料
                from state_store import store
                g = store.get_meta("econ_last_good")
                if g and g.get("at"):
                    good = (float(g["at"]), g)
            except Exception:
                pass
        if good and time.time() - good[0] < 12 * 3600:
            # 2026-10-29：已設金鑰但這次抓取失敗（逾時／限流）→ 沿用最近一次成功的資料，不讓頁面變空
            stale = dict(good[1], stale=True)
            _cache["econ"] = (time.time(), stale)
            return stale
    _cache["econ"] = (time.time(), out)
    if status == "ok":
        _cache["econ_good"] = (time.time(), out)
        try:
            from state_store import store
            store.set_meta("econ_last_good", out)
        except Exception:
            pass
    return out
