"""美國財經日曆（首頁用）。

使用者要求首頁有財經日曆，涵蓋美國所有重要經濟數據：升／降息（FOMC）、大小非農（NFP/ADP）、
初領失業救濟金、CPI、PCE、PPI、零售、GDP、ISM 等，並像富途顯示「前值／預測值／公佈值」。

資料來源採「兩者結合」（使用者選擇）：
  1. 主來源 FMP economic_calendar（需 FMP_API_KEY）：提供事件、前值/預測值/公佈值/重要度。
     FMP 官方 FAQ 明載事件時間為 UTC，這裡統一換算成台北時間（+8）顯示並標註。
  2. 備援/校驗 FRED releases/dates（需 FRED_API_KEY）：美國聖路易聯準會官方「發布排程」，
     只有日期沒有預測值，用來補 FMP 沒涵蓋到的官方發布日（依發布名稱關鍵字比對）。
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

import requests

logger = logging.getLogger(__name__)
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TPE = timezone(timedelta(hours=8))
_UTC = timezone.utc
_cache = {}
_TTL = 3600

# FMP 已於 2024–2025 將 API 遷移到 /stable；新申請的金鑰打舊 /api/v3 會回 403「Legacy Endpoint」，
# 故採官方現行端點。參數（from/to/apikey）與回傳欄位（date/country/event/previous/estimate/actual/impact）
# 與舊版相同，解析不受影響。
FMP_URL = "https://financialmodelingprep.com/stable/economic-calendar"
FRED_DATES_URL = "https://api.stlouisfed.org/fred/releases/dates"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"

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
    """FRED 官方發布排程（只有日期），回傳 [(date_str, release_name)]；未設金鑰或失敗回 []。"""
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
            ds, nm = d.get("date"), d.get("release_name") or ""
            if ds and frm <= ds <= to:
                out.append((ds, nm))
        return out
    except Exception as e:
        logger.warning(f"econ_calendar FRED: {e}")
        return []


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


def fetch_economic_calendar(days_back: int = 1, days_ahead: int = 14, force: bool = False) -> dict:
    """彙整未來美國財經事件。回傳 {events, status, sources, window, at}。
    events 每筆：{dt(UTC iso), date, time, weekday, event, zh, importance(高/中/低),
    prev, estimate, actual, unit, source}。prev/actual 可由 FRED 官方資料庫免費補上（帶 unit 單位），
    estimate（市場共識）無免費來源恆為 None。"""
    if not force:
        c = _cache.get("econ")
        if c and time.time() - c[0] < _TTL:
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

    # 2) 備援/校驗 FRED 官方發布排程（只有日期；補 FMP 沒涵蓋到的官方發布日）
    fred_rows = _fred_future_releases(frm, to)
    if fred_rows:
        sources["fred"] = True
        for ds, nm in fred_rows:
            zh, imp = _match(nm)
            if not zh:                      # 只補我們關心、能對應到中文的官方發布，避免雜訊
                continue
            key = (ds, zh.lower())
            if key in seen:
                continue                    # FMP 已有（有完整數值）→ 不重複、不覆蓋
            seen.add(key)
            try:
                wd = "一二三四五六日"[datetime.strptime(ds, "%Y-%m-%d").weekday()]
            except Exception:
                wd = ""
            events.append({
                "dt": ds, "date": ds, "time": "", "weekday": wd,
                "event": nm, "zh": zh, "importance": imp or "中",
                "prev": None, "estimate": None, "actual": None, "unit": "",
                "source": "FRED",
            })

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
            if not spec or e["prev"] is not None or e["actual"] is not None:
                continue
            series_id, units, unit = spec
            if series_id not in obs_cache:
                obs_cache[series_id] = _fred_obs_latest(series_id, units)
            obs = obs_cache[series_id]
            if not obs:
                continue
            if e["date"] <= today:                       # 已公佈：最新＝公佈值、次新＝前值
                e["actual"] = obs[0][1]
                e["prev"] = obs[1][1] if len(obs) > 1 else None
                e["unit"] = unit
                e["source"] = "FRED"
            elif series_id not in future_taken:           # 最近一筆未來事件：前值＝最後公佈
                e["prev"] = obs[0][1]
                e["unit"] = unit
                e["source"] = "FRED"
                future_taken.add(series_id)

    status = "ok" if (sources["fmp"] or sources["fred"]) else "no_source"
    out = {"events": events, "status": status, "sources": sources,
           "window": {"from": frm, "to": to}, "tz": "Asia/Taipei", "at": time.time()}
    _cache["econ"] = (time.time(), out)
    return out
