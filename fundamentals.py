"""
fundamentals.py — 個股基本面資料（月營收）v1.0
★ 新增：2026-09-24——使用者要求「不要只看K線，波段交易不能只有純技術面」，
指定先從「個股基本面/公告」下手，且要當成硬性過濾條件（不是加減分）。

這裡先做風險最低、資料結構最穩定的一塊：TWSE OpenAPI 的上市公司月營收
（已用 WebFetch 實際查證過欄位名稱，見開發過程紀錄），上櫃(TPEx)月營收因為
這個專案已經證實 tpex.org.tw 網域整體架在 Cloudflare 之後（見 data_fetcher.py
_fetch_tpex() 的詳細除錯記錄），採用同一套 curl_cffi 偽裝瀏覽器的作法嘗試，
抓不到就優雅降級（那些個股不套用基本面過濾，不會因為抓不到資料就誤傷）。

「重大訊息公告」（地雷公告）的部分，使用者已表示之後再評估要不要加新聞來源，
這裡先不做——避免用不可靠的關鍵字分類把好訊號誤殺，同時在下面留下清楚的
TODO 說明，避免以後有人以為這塊已經做了。
"""
import time, logging, requests
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

_cache: Dict = {}
_CACHE_TTL_SEC = 3600 * 12  # 月營收一個月才更新一次，12小時已經很保守

def _cache_get(key):
    e = _cache.get(key)
    return e["data"] if e and time.time() - e["ts"] < _CACHE_TTL_SEC else None

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}
    return data


def _fetch_twse_monthly_revenue() -> Dict[str, Dict]:
    """上市公司月營收，TWSE OpenAPI（已實際查證欄位名稱）。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_monthly_revenue: HTTP {r.status_code}")
            return {}
        rows = r.json()
        result = {}
        for row in rows:
            code = (row.get("公司代號") or "").strip()
            if not code:
                continue
            try:
                yoy = float(row.get("營業收入-去年同月增減(%)") or 0)
            except (TypeError, ValueError):
                yoy = None
            try:
                mom = float(row.get("營業收入-上月比較增減(%)") or 0)
            except (TypeError, ValueError):
                mom = None
            result[code] = {
                "yoy_pct": yoy, "mom_pct": mom,
                "period": row.get("資料年月", ""), "source": "twse_openapi",
            }
        logger.info(f"月營收（上市）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_monthly_revenue: {e}")
        return {}


def _fetch_tpex_monthly_revenue() -> Dict[str, Dict]:
    """上櫃公司月營收，TPEx OpenAPI。這個網域已知架在 Cloudflare 之後（見
    data_fetcher._fetch_tpex() 的詳細除錯記錄），用 curl_cffi 偽裝瀏覽器
    TLS 指紋嘗試，失敗就回空字典（優雅降級，不阻擋任何訊號）。"""
    url = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, headers=HEADERS, timeout=15, impersonate="chrome", verify=False)
        if r.status_code != 200:
            logger.warning(f"_fetch_tpex_monthly_revenue: HTTP {r.status_code}（已知 TPEx 有 Cloudflare 保護，失敗時不影響上市個股的過濾，僅上櫃個股不套用基本面過濾）")
            return {}
        rows = r.json()
        result = {}
        for row in rows:
            code = (row.get("公司代號") or "").strip()
            if not code:
                continue
            try:
                yoy = float(row.get("營業收入-去年同月增減(%)") or 0)
            except (TypeError, ValueError):
                yoy = None
            result[code] = {"yoy_pct": yoy, "mom_pct": None,
                             "period": row.get("資料年月", ""), "source": "tpex_openapi"}
        logger.info(f"月營收（上櫃）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_tpex_monthly_revenue: {e}（上櫃個股本次不套用基本面過濾）")
        return {}


def fetch_monthly_revenue_map() -> Dict[str, Dict]:
    """回傳 {公司代號: {yoy_pct, mom_pct, period, source}}，涵蓋上市+上櫃。
    任一來源失敗只影響該來源涵蓋的個股，不會讓另一邊也連帶失效。"""
    if c := _cache_get("monthly_revenue"):
        return c
    merged = {}
    merged.update(_fetch_twse_monthly_revenue())
    merged.update(_fetch_tpex_monthly_revenue())
    return _cache_set("monthly_revenue", merged)


# ★ 新增：2026-09-24——硬性過濾門檻。營收年增率 <= -30% 代表本業明顯衰退，
# 不管技術面分數多高，都不該被當成「波段做多」的候選——技術面的量價訊號
# 可能只是短線反彈/軋空，不是基本面轉強。門檻刻意保守（-30%是相當嚴重的
# 衰退，不是「小幅衰退」就排除），避免誤殺太多原本合理的訊號；之後有更多
# 實際結算資料，可以再依實際勝率差異調整這個數字。
REVENUE_YOY_HARD_FLOOR = -30.0


def check_fundamental_hard_filter(code: str, revenue_map: Optional[Dict] = None) -> Dict:
    """回傳 {"blocked": bool, "reason": str|None, "yoy_pct": float|None}。
    找不到資料（新股、資料源當天失敗等）一律不擋，「不知道」不等於「有問題」，
    跟這個專案其他過濾邏輯（sector/相關性群組）的既有原則一致。"""
    revenue_map = revenue_map if revenue_map is not None else fetch_monthly_revenue_map()
    info = revenue_map.get(code)
    if not info or info.get("yoy_pct") is None:
        return {"blocked": False, "reason": None, "yoy_pct": None}
    yoy = info["yoy_pct"]
    if yoy <= REVENUE_YOY_HARD_FLOOR:
        return {"blocked": True, "yoy_pct": yoy,
                "reason": f"月營收年增率 {yoy:+.1f}%（{info.get('period','')}），本業明顯衰退，不列入波段候選"}
    return {"blocked": False, "reason": None, "yoy_pct": yoy}


# ── TODO（使用者已表示之後再評估，先不做）──
# 重大訊息公告（地雷公告：跳票、重整、下市、財報重編等）目前沒有可靠、
# 低成本的結構化資料源可以直接判斷「這則公告是不是地雷」，需要文字分類/NLP，
# 準確度沒有把握前，貿然接上去可能誤殺正常公告、或漏掉真正的地雷，比不做
# 更危險。若之後要做，建議先串接公開資訊觀測站(MOPS)重大訊息查詢 API，
# 並且先用歷史地雷股名單驗證分類準確度，而不是直接上線影響訊號產生。
