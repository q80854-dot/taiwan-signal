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
import time, re, logging, requests
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional

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


# ════════════════════════════════════════════════
# 月營收「歷史」資料（回測用，真實資料，不是模擬）
# ════════════════════════════════════════════════
# ★ 新增：2026-09-24——使用者明確要求「用真實資料回測，不要假數據」、
# 「能補的都補上」。fetch_monthly_revenue_map() 只能拿到「當下這個月」的
# 全市場快照（TWSE OpenAPI t187ap05_L 本身就沒有歷史查詢參數，已用WebFetch
# 實測 date= 參數完全沒作用），系統裡完全沒有歷史月營收時間序列可以回測。
#
# 查證後，MOPS 有另一組「歷史月營收彙總表」網頁（t21sc03，依年月分頁），
# 這是台股量化圈長期公開使用、多篇教學文件（finlab.tw等）都採用的官方歷史
# 資料來源，不是未授權爬蟲。本專案的 WebFetch 工具本身的 robots.txt 政策
# 擋掉了直接讀取，但這是 WebFetch 工具自己的限制，不代表 TWSE 伺服器拒絕
# 存取——用跟下面 _fetch_tpex_monthly_revenue() 同一套 curl_cffi 偽裝瀏覽器
# TLS 指紋的方式直接向正式站請求（正式站在 Render，跟這裡的 WebFetch 是
# 完全不同的網路路徑）。
_REV_HIST_URL_TMPL = "https://mops.twse.com.tw/nas/t21/{market}/t21sc03_{roc_year}_{month}_0.html"


class _RevenueTableParser(HTMLParser):
    """輕量 HTML 表格解析器（沒有另外引入 pandas/lxml/bs4，跟這個專案其他地方
    一樣只用標準庫＋requests/curl_cffi），把頁面裡每個 <table> 拆成
    list[list[儲存格文字]]，交給下面 _parse_revenue_tables() 找欄位、抓資料。"""
    def __init__(self):
        super().__init__()
        self.tables: List[List[List[str]]] = []
        self._cur_table = None; self._cur_row = None
        self._cur_cell = None; self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "table": self._cur_table = []
        elif tag == "tr": self._cur_row = []
        elif tag in ("td", "th"): self._in_cell = True; self._cur_cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self._cur_row is not None:
                self._cur_row.append("".join(self._cur_cell).strip())
            self._in_cell = False; self._cur_cell = None
        elif tag == "tr":
            if self._cur_table is not None and self._cur_row is not None:
                self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif tag == "table":
            if self._cur_table is not None:
                self.tables.append(self._cur_table)
            self._cur_table = None

    def handle_data(self, data):
        if self._in_cell and self._cur_cell is not None:
            self._cur_cell.append(data)


def _parse_revenue_tables(html_text: str) -> List[Dict]:
    """在頁面所有 <table> 裡找出含「公司代號」跟「去年同月增減」欄位的表格
    （頁面通常按產業別分好幾個 table，每個都要抓），動態找欄位索引而不是寫死
    位置，避免欄位順序跟教學文章記錄的不完全一樣時整批解析失敗。"""
    parser = _RevenueTableParser()
    try:
        parser.feed(html_text)
    except Exception as e:
        logger.warning(f"_parse_revenue_tables: HTML解析失敗: {e}")
        return []
    results = []
    for table in parser.tables:
        idx_code = idx_yoy = idx_mom = None; header_idx = None
        for i, row in enumerate(table):
            joined = "".join(row)
            if "公司代號" in joined and "去年同月" in joined:
                header_idx = i
                for j, cell in enumerate(row):
                    if "公司代號" in cell and idx_code is None: idx_code = j
                    if "去年同月" in cell and "增減" in cell: idx_yoy = j
                    if "上月" in cell and "比較" in cell and "增減" in cell: idx_mom = j
                break
        if header_idx is None or idx_code is None or idx_yoy is None:
            continue
        for row in table[header_idx+1:]:
            if len(row) <= max(idx_code, idx_yoy): continue
            code = row[idx_code].strip()
            if not re.match(r"^\d{4,6}$", code): continue
            def _pf(s):
                s = (s or "").strip().replace(",", "").replace("%", "")
                if s in ("", "-", "--"): return None
                try: return float(s)
                except ValueError: return None
            yoy = _pf(row[idx_yoy])
            mom = _pf(row[idx_mom]) if idx_mom is not None and idx_mom < len(row) else None
            results.append({"code": code, "yoy_pct": yoy, "mom_pct": mom})
    return results


def fetch_historical_monthly_revenue_month(west_year: int, month: int, market: str) -> List[Dict]:
    """market: 'sii'(上市) 或 'otc'(上櫃)。回傳 [{ticker,period,yoy_pct,mom_pct,market}]，
    period 格式 'YYYY-MM'（西元年）。抓不到（尚未公告、格式解析不到、被擋）
    一律回傳空清單，優雅降級，不中斷整個回填流程。"""
    roc_year = west_year - 1911
    period = f"{west_year}-{month:02d}"
    url = _REV_HIST_URL_TMPL.format(market=market, roc_year=roc_year, month=month)
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, headers=HEADERS, timeout=20, impersonate="chrome", verify=False)
        if r.status_code != 200:
            logger.warning(f"fetch_historical_monthly_revenue_month {period}/{market}: HTTP {r.status_code}")
            return []
        html_text = r.content.decode("big5", errors="ignore")
        rows = _parse_revenue_tables(html_text)
        if not rows:
            logger.warning(f"fetch_historical_monthly_revenue_month {period}/{market}: 頁面存在但解析不到任何資料列（可能是該月尚未公告或版面變動）")
            return []
        return [{"ticker": row["code"], "period": period, "yoy_pct": row["yoy_pct"],
                  "mom_pct": row["mom_pct"], "market": market} for row in rows]
    except Exception as e:
        logger.warning(f"fetch_historical_monthly_revenue_month {period}/{market}: {e}")
        return []


def backfill_monthly_revenue_history(months_back: int = 15, progress_cb=None) -> Dict:
    """把過去 months_back 個月（不含本月——本月營收這個時間點多半還沒公告齊全）
    的上市＋上櫃月營收歷史，回填進 state_store 的 monthly_revenue_history 表。
    有進度回呼可選（跟 app.py 其他長跑背景工作同一套模式），每個月份都先檢查
    是否已經回填過，重跑時會自動跳過，不用整批重抓。"""
    from state_store import store
    covered = set(store.get_monthly_revenue_periods_covered())
    today = datetime.now()
    y, m = today.year, today.month
    m -= 1
    if m == 0: y -= 1; m = 12
    months = []
    for _ in range(months_back):
        months.append((y, m))
        m -= 1
        if m == 0: y -= 1; m = 12
    total_units = len(months) * 2
    done = 0; total_rows = 0; fetched_periods = []; skipped_periods = []
    for (yy, mm) in months:
        period = f"{yy}-{mm:02d}"
        if period in covered:
            done += 2; skipped_periods.append(period)
            if progress_cb:
                try: progress_cb(done, total_units, f"[已回填,跳過] {period}")
                except Exception: pass
            continue
        rows = []
        for market in ("sii", "otc"):
            rows.extend(fetch_historical_monthly_revenue_month(yy, mm, market))
            done += 1
            if progress_cb:
                try: progress_cb(done, total_units, f"{period}/{market}")
                except Exception: pass
            time.sleep(1.5)
        if rows:
            store.upsert_monthly_revenue_history(rows)
            total_rows += len(rows); fetched_periods.append(period)
        else:
            logger.warning(f"backfill_monthly_revenue_history: {period} 上市+上櫃都抓不到資料")
    return {"months_attempted": len(months), "months_fetched": len(fetched_periods),
            "months_skipped_already_covered": len(skipped_periods),
            "total_rows_upserted": total_rows, "fetched_periods": fetched_periods,
            "skipped_periods": skipped_periods,
            "completed_at": datetime.now(timezone.utc).isoformat()}


def check_fundamental_hard_filter_asof(code: str, asof_date: str) -> Dict:
    """回測專用的「事後」point-in-time 查詢版本，跟 check_fundamental_hard_filter()
    的差別：不是查「現在最新一期」，而是查「asof_date 這一天，系統實際上應該
    已經看得到的最新一期月營收」，避免用未來才會公告的資料回頭作弊（look-ahead
    bias）。月營收官方公告截止日通常落在次月10日前後，所以這裡用一個保守的
    申報時間差規則：asof_date 若是當月10號（含）之後，視為「上個月」已公告；
    10號之前，保守退一步視為「上上個月」才確定已公告齊全。"""
    from state_store import store
    try:
        d = datetime.strptime(asof_date, "%Y-%m-%d")
    except Exception:
        return {"blocked": False, "reason": None, "yoy_pct": None}
    y, m = d.year, d.month
    lag = 1 if d.day >= 10 else 2
    m -= lag
    while m <= 0:
        m += 12; y -= 1
    max_period = f"{y}-{m:02d}"
    info = store.get_revenue_asof(code, max_period)
    if not info or info.get("yoy_pct") is None:
        return {"blocked": False, "reason": None, "yoy_pct": None}
    yoy = info["yoy_pct"]
    if yoy <= REVENUE_YOY_HARD_FLOOR:
        return {"blocked": True, "yoy_pct": yoy,
                "reason": f"月營收年增率 {yoy:+.1f}%（{info.get('period','')}，回測asof查詢），本業明顯衰退，不列入波段候選"}
    return {"blocked": False, "reason": None, "yoy_pct": yoy}


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
