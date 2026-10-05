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

_TTL_OVERRIDE = {"material_news_risk_map": 900, "material_news_fetch_ok": 900, "all_news_rows": 600}  # 重大訊息 10~15 分鐘更新

def _cache_get(key):
    e = _cache.get(key)
    ttl = _TTL_OVERRIDE.get(key, _CACHE_TTL_SEC)
    return e["data"] if e and time.time() - e["ts"] < ttl else None

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}
    return data


def _num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _rev_row(row: Dict, source: str) -> Dict:
    """月營收單列：除了單月年增／月增，另外保留當月營收金額（千元）與累計年增，
    供基本面評分判斷「這個月只是一次性衝高，還是整年都在成長」。"""
    return {
        "yoy_pct": _num(row.get("營業收入-去年同月增減(%)")),
        "mom_pct": _num(row.get("營業收入-上月比較增減(%)")),
        "cum_yoy_pct": _num(row.get("累計營業收入-前期比較增減(%)")),
        "revenue": _num(row.get("營業收入-當月營收")),
        "revenue_ly": _num(row.get("營業收入-去年當月營收")),
        "cum_revenue": _num(row.get("累計營業收入-當月累計營收")),
        "name": (row.get("公司名稱") or "").strip(),
        "sector_name": (row.get("產業別") or "").strip(),
        "period": row.get("資料年月", ""), "source": source,
    }


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
            result[code] = _rev_row(row, "twse_openapi")
        logger.info(f"月營收（上市）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_monthly_revenue: {e}")
        return {}


def _fetch_tpex_monthly_revenue() -> Dict[str, Dict]:
    """上櫃公司月營收，TPEx OpenAPI。
    ★ 修正：2026-09-27——推翻原本「TPEx 整個網域架在 Cloudflare 之後，需要
    curl_cffi 偽裝瀏覽器 TLS 指紋才能繞過」的結論（同樣的錯誤判斷也出現在
    data_fetcher._fetch_tpex()，已在該處修正並記錄完整除錯過程）。實測從
    Render 伺服器直接用 requests.get() 打 www.tpex.org.tw/openapi/v1/...
    是通的，403 只出現在 WebFetch 工具自己的請求特徵上。這裡改成 requests
    優先，curl_cffi 降為次要備援（萬一哪天 requests 這個路徑真的被擋，還有
    一層保底，不會整個功能一次失效）。"""
    url = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

    def _parse(rows) -> Dict[str, Dict]:
        result = {}
        for row in rows:
            code = (row.get("公司代號") or "").strip()
            if not code:
                continue
            result[code] = _rev_row(row, "tpex_openapi")
        return result

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            result = _parse(r.json())
            logger.info(f"月營收（上櫃）：{len(result)} 檔")
            return result
        logger.warning(f"_fetch_tpex_monthly_revenue: requests HTTP {r.status_code}，改試 curl_cffi 備援")
    except Exception as e:
        logger.warning(f"_fetch_tpex_monthly_revenue requests: {e}，改試 curl_cffi 備援")

    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, headers=HEADERS, timeout=15, impersonate="chrome", verify=False)
        if r.status_code != 200:
            logger.warning(f"_fetch_tpex_monthly_revenue: curl_cffi 也失敗 HTTP {r.status_code}（上櫃個股本次不套用基本面過濾）")
            return {}
        result = _parse(r.json())
        logger.info(f"月營收（上櫃，curl_cffi 備援）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_tpex_monthly_revenue curl_cffi: {e}（上櫃個股本次不套用基本面過濾）")
        return {}


# ── 月營收即時性 ────────────────────────────────────────────
# 2026-10-05 使用者反映：鴻海 9 月營收已公布，系統卻還顯示 8 月。原因：①快取 12 小時、
# ②官方 OpenAPI 的更新常晚於公司公告。改為：每月 1～15 日（公告期，法定截止日 10 日）
# 快取只留 10 分鐘，且最新一期還沒到齊時一律視為過期；其餘日期 6 小時。
# 另外在 OpenAPI 還沒給出新一期時，改用公開資訊觀測站（MOPS）彙總表補上（見 _fetch_mops_period）。
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
_rev_state: Dict = {"fetched_at": None, "expected": None, "n_expected": 0, "n_total": 0, "mops_added": 0, "last_error": None}


import threading as _threading
_rev_bg_lock = _threading.Lock()


def _taipei_now():
    return _dt.now(_tz.utc) + _td(hours=8)


def expected_revenue_period(now=None) -> str:
    """目前『應該已經可以看到』的最新一期（上個月），民國格式如 '11509'。"""
    n = now or _taipei_now()
    y, m = n.year, n.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y - 1911}{m:02d}"


def _rev_ttl() -> int:
    n = _taipei_now()
    if n.day <= 15:
        return 600
    return 3600 * 6


def _rev_cache_fresh() -> bool:
    e = _cache.get("monthly_revenue")
    if not e:
        return False
    if time.time() - e["ts"] >= _rev_ttl():
        return False
    return True


def _num_or_none(s):
    s = (s or "").strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_mops_full(html_text: str) -> List[Dict]:
    """解析 MOPS 月營收彙總表（t21sc03），除了年增月增，也取得營收金額（千元）與累計。欄位用標題文字動態對應。"""
    parser = _RevenueTableParser()
    try:
        parser.feed(html_text)
    except Exception:
        return []
    out = []
    for table in parser.tables:
        hdr = None
        for i, row in enumerate(table):
            j = "".join(row)
            if "公司代號" in j and "去年同月" in j:
                hdr = i
                break
        if hdr is None:
            continue
        H = table[hdr]
        def find(*keys, excl=()):
            for k, c in enumerate(H):
                if all(x in c for x in keys) and not any(x in c for x in excl):
                    return k
            return None
        ic = find("公司代號"); iname = find("公司名稱")
        i_cur = find("當月營收", excl=("去年", "累計")); i_ly = find("去年當月營收")
        i_mom = find("上月比較"); i_yoy = find("去年同月")
        i_cum = find("當月累計營收"); i_cumyoy = find("前期比較")
        if ic is None or i_yoy is None or i_cur is None:
            continue
        need = max(x for x in (ic, i_yoy, i_cur, i_ly or 0, i_mom or 0, i_cum or 0, i_cumyoy or 0))
        for row in table[hdr + 1:]:
            if len(row) <= need:
                continue
            code = row[ic].strip()
            if not re.match(r"^\d{4,6}$", code):
                continue
            g = lambda k: _num_or_none(row[k]) if k is not None and k < len(row) else None
            out.append({"code": code, "name": row[iname].strip() if iname is not None else "",
                        "yoy_pct": g(i_yoy), "mom_pct": g(i_mom), "cum_yoy_pct": g(i_cumyoy),
                        "revenue": g(i_cur), "revenue_ly": g(i_ly), "cum_revenue": g(i_cum)})
    return out


def _fetch_mops_period(period_roc: str) -> Dict[str, Dict]:
    """從 MOPS 抓某一期（如 '11509'）全市場月營收；任何失敗回傳空 dict（OpenAPI 仍是主要來源）。"""
    roc, mm = int(period_roc[:-2]), int(period_roc[-2:])
    res = {}
    for market in ("sii", "otc"):
        try:
            from curl_cffi import requests as cffi_requests
            url = _REV_HIST_URL_TMPL.format(market=market, roc_year=roc, month=mm)
            r = cffi_requests.get(url, headers=HEADERS, timeout=20, impersonate="chrome", verify=False)
            if r.status_code != 200:
                logger.info(f"MOPS 月營收 {period_roc}/{market}: HTTP {r.status_code}（可能尚未產生）")
                continue
            for row in _parse_mops_full(r.content.decode("big5", errors="ignore")):
                row.update({"period": period_roc, "source": "mops_" + market, "sector_name": ""})
                res[row["code"]] = row
        except Exception as e:
            logger.warning(f"_fetch_mops_period {period_roc}/{market}: {e}")
    return res


def fetch_monthly_revenue_map(force: bool = False) -> Dict[str, Dict]:
    """回傳 {公司代號: {yoy_pct, mom_pct, period, source...}}，涵蓋上市+上櫃。
    任一來源失敗只影響該來源涵蓋的個股，不會讓另一邊也連帶失效。
    公告期（每月 1～15 日）快取 10 分鐘；官方 OpenAPI 尚未更新到最新一期時，用 MOPS 補上。"""
    if not force and _rev_cache_fresh():
        return _cache["monthly_revenue"]["data"]
    if not force and _cache.get("monthly_revenue"):
        # 過期但有舊資料：先回舊的（使用者不用等 10 幾秒），同時在背景更新（同時只會有一個）
        if _rev_bg_lock.acquire(blocking=False):
            def _bg():
                try:
                    fetch_monthly_revenue_map(force=True)
                except Exception as e:
                    logger.warning(f"月營收背景更新失敗: {e}")
                finally:
                    _rev_bg_lock.release()
            import threading as _th
            _th.Thread(target=_bg, daemon=True).start()
        return _cache["monthly_revenue"]["data"]
    merged = {}
    merged.update(_fetch_twse_monthly_revenue())
    merged.update(_fetch_tpex_monthly_revenue())
    exp = expected_revenue_period()
    n_exp = sum(1 for v in merged.values() if str(v.get("period")) == exp)
    added = 0
    # 公告期內，且最新一期家數明顯不足（OpenAPI 還沒更新完）才去補 MOPS
    if _taipei_now().day <= 15 and merged and n_exp < 0.9 * len(merged):
        for code, row in _fetch_mops_period(exp).items():
            cur = merged.get(code)
            if cur and str(cur.get("period")) == exp:
                continue
            if row.get("yoy_pct") is None and row.get("revenue") is None:
                continue
            if cur:
                row["sector_name"] = cur.get("sector_name", "")
                row["name"] = row.get("name") or cur.get("name", "")
            merged[code] = row
            added += 1
        if added:
            logger.info(f"月營收：OpenAPI 尚缺 {exp} 期，由 MOPS 補上 {added} 檔")
    _rev_state.update({"fetched_at": time.time(), "expected": exp,
                       "n_expected": sum(1 for v in merged.values() if str(v.get("period")) == exp),
                       "n_total": len(merged), "mops_added": added})
    if not merged and _cache.get("monthly_revenue"):
        return _cache["monthly_revenue"]["data"]   # 兩邊都抓失敗：沿用舊資料，下次再試
    return _cache_set("monthly_revenue", merged)


def refresh_monthly_revenue() -> List[Dict]:
    """強制重抓並回傳『最新一期有變動』的公司清單（新公布，或期別往前進）。"""
    old = (_cache.get("monthly_revenue") or {}).get("data") or {}
    new = fetch_monthly_revenue_map(force=True)
    changed = []
    for code, v in new.items():
        o = old.get(code)
        if not o or str(v.get("period")) > str(o.get("period")):
            changed.append({"code": code, "name": v.get("name", ""), "period": v.get("period"),
                            "yoy_pct": v.get("yoy_pct"), "revenue": v.get("revenue"), "first": not o})
    if not old:
        return []   # 冷啟動第一次載入，不當作『新公布』
    return changed


def revenue_status(code: str = "") -> Dict:
    """給個股研究頁用：顯示這檔的營收是否已是最新一期、資料取得時間、法定公告期限。"""
    exp = expected_revenue_period()
    cur = (fetch_monthly_revenue_map().get(code) or {}) if code else {}
    have = str(cur.get("period") or "")
    n = _taipei_now()
    return {"expected_period": exp, "have_period": have or None, "is_latest": have >= exp if have else False,
            "fetched_at": _rev_state.get("fetched_at"), "n_expected": _rev_state.get("n_expected"),
            "n_total": _rev_state.get("n_total"), "mops_added": _rev_state.get("mops_added"),
            "source": cur.get("source"), "in_window": n.day <= 15,
            "deadline": f"{n.year}/{n.month:02d}/10 前（公開發行公司法定申報截止日）" if n.day <= 15 else ""}


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
# ★ 修正：2026-09-25——部署後實測回填15個月×2個市場全部回傳HTTP 404
# （production log 全部命中，不是「部分月份尚未公告」那種零星404，是
# 100%命中，代表網址本身就錯）。用WebFetch直接測試 mops.twse.com.tw
# 這個domain（新版MOPS）的t21路徑，2024/2025年份的月份一樣404，判斷
# domain本身錯誤；改用WebSearch交叉比對，找到多筆搜尋引擎確實索引到
# mopsov.twse.com.tw（「舊版」公開資訊觀測站）這個domain下同樣路徑的
# 真實存在頁面（包含精確比對過的113_1、114_11、115_4等年月，含sii跟
# otc兩個市場），確認這組歷史月營收彙總表其實是掛在舊版MOPS
# （mopsov.twse.com.tw）底下，不是新版（mops.twse.com.tw）——這是
# 先前查證時的判斷錯誤。mopsov.twse.com.tw本身會被WebFetch工具自己的
# robots.txt政策擋掉（見上方說明，這是WebFetch工具限制，不代表伺服器端
# 真的拒絕），所以沒辦法用WebFetch直接驗證回應內容，改成部署後直接用正式站
# 的curl_cffi實測（走完全不同的網路路徑）驗證是否真的能拿到資料。
_REV_HIST_URL_TMPL = "https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{roc_year}_{month}_0.html"


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
        # ★ 修正：2026-09-25——見上方2026-09-25診斷紀錄：真實MOPS頁面偶爾有
        # 不成對的收尾標籤（例如多一個孤立的</td>，html.parser本身不會驗證
        # 標籤是否成對），導致這裡收到</td>時self._cur_cell其實已經是None
        # （不在任何<td>裡面），原本"".join(self._cur_cell)對None呼叫join
        # 直接丟TypeError("can only join an iterable")，被外層_parse_revenue_
        #_tables()的except吞掉變成「解析失敗」，白白浪費掉一個原本抓到34個
        # table、內容完全正常的頁面。這裡加防呆：cur_cell是None時就跳過，不
        # 讓一個不成對標籤拖垮整頁解析。
        if tag in ("td", "th"):
            if self._cur_row is not None and self._cur_cell is not None:
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


_rev_diag_logged = 0  # ★ 新增：2026-09-25——domain改對(mopsov)後不再404，但解析不到
# 任何資料列，需要一次性看清楚真實HTML長什麼樣才能修解析邏輯。只記錄前2次，
# 避免洗版log（15個月×2市場=30次呼叫，不需要每次都印）。

def fetch_historical_monthly_revenue_month(west_year: int, month: int, market: str) -> List[Dict]:
    """market: 'sii'(上市) 或 'otc'(上櫃)。回傳 [{ticker,period,yoy_pct,mom_pct,market}]，
    period 格式 'YYYY-MM'（西元年）。抓不到（尚未公告、格式解析不到、被擋）
    一律回傳空清單，優雅降級，不中斷整個回填流程。"""
    global _rev_diag_logged
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
            if _rev_diag_logged < 2:
                _rev_diag_logged += 1
                parser = _RevenueTableParser()
                try:
                    parser.feed(html_text)
                except Exception as e:
                    logger.warning(f"fetch_historical_monthly_revenue_month {period}/{market} 診斷: HTMLParser本身丟例外: {e}")
                has_code_kw = "公司代號" in html_text
                has_yoy_kw = "去年同月" in html_text
                idx = html_text.find("公司代號")
                snippet = html_text[max(0, idx-50):idx+300] if idx >= 0 else "(找不到「公司代號」這個關鍵字)"
                first_rows_preview = []
                for ti, table in enumerate(parser.tables[:5]):
                    first_rows_preview.append(f"table{ti}(共{len(table)}列): 第0列={table[0] if table else '(空)'}")
                logger.warning(
                    f"fetch_historical_monthly_revenue_month {period}/{market} 診斷: "
                    f"html長度={len(html_text)}，含「公司代號」={has_code_kw}，含「去年同月」={has_yoy_kw}，"
                    f"parser找到{len(parser.tables)}個table\n"
                    f"「公司代號」附近原文片段: {snippet!r}\n"
                    f"各table第0列預覽: {first_rows_preview}"
                )
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
        return {"blocked": False, "reason": None, "yoy_pct": None, "data_missing": True}
    yoy = info["yoy_pct"]
    if yoy <= REVENUE_YOY_HARD_FLOOR:
        return {"blocked": True, "yoy_pct": yoy, "data_missing": False,
                "reason": f"月營收年增率 {yoy:+.1f}%（{info.get('period','')}，回測asof查詢），本業明顯衰退，不列入波段候選"}
    return {"blocked": False, "reason": None, "yoy_pct": yoy, "data_missing": False}


# ★ 新增：2026-09-24——硬性過濾門檻。營收年增率 <= -30% 代表本業明顯衰退，
# 不管技術面分數多高，都不該被當成「波段做多」的候選——技術面的量價訊號
# 可能只是短線反彈/軋空，不是基本面轉強。門檻刻意保守（-30%是相當嚴重的
# 衰退，不是「小幅衰退」就排除），避免誤殺太多原本合理的訊號；之後有更多
# 實際結算資料，可以再依實際勝率差異調整這個數字。
REVENUE_YOY_HARD_FLOOR = -30.0


# ★ 修正：2026-09-28——把這個「找不到資料一律不擋」的 fail-open 設計貼給
# ChatGPT／Perplexity 審查，兩邊都把它列為目前系統優先級最高的風險項目之一：
# fail-open 本身的方向（資料源故障時不要錯殺）沒有錯，但原本的回傳值讓
# 「查過、確認乾淨」跟「根本沒查到」在下游（scanner.py／Telegram推播）完全
# 無法分辨——兩者都是 blocked=False，一檔真正該被排除的地雷股，如果剛好那天
# 資料源抓不到它的月營收，會跟一檔「月營收年增率經過確認是+20%」的健康股票
# 顯示得一模一樣，使用者拿到訊號時完全看不出這檔其實沒有被基本面把關過。
# 這裡新增 data_missing 欄位，不改變「不擋」這個行為本身（仍然是 fail-open，
# 不是改成 fail-closed 誤殺資料源故障的正常股票），但讓下游可以把「沒查到」
# 明確標示出來，而不是讓它悄悄跟「查過沒事」變成同一種外觀。
def check_fundamental_hard_filter(code: str, revenue_map: Optional[Dict] = None) -> Dict:
    """回傳 {"blocked": bool, "reason": str|None, "yoy_pct": float|None, "data_missing": bool}。
    找不到資料（新股、資料源當天失敗等）一律不擋，「不知道」不等於「有問題」，
    跟這個專案其他過濾邏輯（sector/相關性群組）的既有原則一致——但會用
    data_missing=True 明確標示「這筆沒有真的被檢查過」，避免跟「檢查過確認
    正常」的情況混在一起、讓使用者誤以為所有訊號都經過月營收把關。"""
    revenue_map = revenue_map if revenue_map is not None else fetch_monthly_revenue_map()
    info = revenue_map.get(code)
    if not info or info.get("yoy_pct") is None:
        return {"blocked": False, "reason": None, "yoy_pct": None, "data_missing": True}
    yoy = info["yoy_pct"]
    if yoy <= REVENUE_YOY_HARD_FLOOR:
        return {"blocked": True, "yoy_pct": yoy, "data_missing": False,
                "reason": f"月營收年增率 {yoy:+.1f}%（{info.get('period','')}），本業明顯衰退，不列入波段候選"}
    return {"blocked": False, "reason": None, "yoy_pct": yoy, "data_missing": False}


# ── TODO（使用者已表示之後再評估，先不做）──
# 重大訊息公告（地雷公告：跳票、重整、下市、財報重編等）目前沒有可靠、
# 低成本的結構化資料源可以直接判斷「這則公告是不是地雷」，需要文字分類/NLP，
# 準確度沒有把握前，貿然接上去可能誤殺正常公告、或漏掉真正的地雷，比不做
# 更危險。若之後要做，建議先串接公開資訊觀測站(MOPS)重大訊息查詢 API，
# 並且先用歷史地雷股名單驗證分類準確度，而不是直接上線影響訊號產生。


# ════════════════════════════════════════════════
# 估值面（本益比/股價淨值比/殖利率）
# ════════════════════════════════════════════════
# ★ 新增：2026-09-27——使用者要求補上「估值面(本益比/股價淨值比/殖利率) +
# 獲利品質」。估值面這塊直接有官方 OpenAPI 逐股資料，兩個市場都已實測確認
# 欄位名稱（見 app.py /api/diagnostics/probe_openapi 的探測記錄）：
#   TWSE: openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL
#         欄位（英文）：Code, Name, PEratio, DividendYield, PBratio
#   TPEx: www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis
#         欄位（英文）：SecuritiesCompanyCode, CompanyName, PriceEarningRatio,
#         DividendPerShare, YieldRatio, PriceBookRatio
# 「獲利品質」（毛利率/營益率趨勢）需要季報財務資料，欄位/計算方式跟月營收
# 硬性過濾不是同一個等級的資料源穩定度，先不在這次一起做，留在下面
# fetch_valuation_map() 之後單獨評估（見檔案最後的 TODO）。
def _f(v) -> Optional[float]:
    """安全轉 float：空字串/None/千分位逗號一律轉成 None，不是 0——
    「沒有資料」跟「真的是0」是兩回事，跟這個檔案其他地方的既有原則一致。"""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fetch_twse_valuation() -> Dict[str, Dict]:
    """上市個股本益比/殖利率/股價淨值比，TWSE OpenAPI（依代碼查詢，全市場一次回傳）。"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_valuation: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code = (row.get("Code") or "").strip()
            if not code:
                continue
            result[code] = {
                "pe": _f(row.get("PEratio")),
                "yield_pct": _f(row.get("DividendYield")),
                "pb": _f(row.get("PBratio")),
                "source": "twse_openapi",
            }
        logger.info(f"估值面（上市）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_valuation: {e}")
        return {}


def _fetch_tpex_valuation() -> Dict[str, Dict]:
    """上櫃個股本益比/殖利率/股價淨值比，TPEx OpenAPI。"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_tpex_valuation: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code = (row.get("SecuritiesCompanyCode") or "").strip()
            if not code:
                continue
            result[code] = {
                "pe": _f(row.get("PriceEarningRatio")),
                "yield_pct": _f(row.get("YieldRatio")),
                "pb": _f(row.get("PriceBookRatio")),
                "source": "tpex_openapi",
            }
        logger.info(f"估值面（上櫃）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_tpex_valuation: {e}")
        return {}


def fetch_valuation_map() -> Dict[str, Dict]:
    """回傳 {代號: {pe, yield_pct, pb, source}}，涵蓋上市+上櫃。任一來源失敗
    只影響該來源涵蓋的個股（優雅降級，跟 fetch_monthly_revenue_map() 同一個
    設計原則），快取沿用本檔案模組層級的 _cache（12小時，估值資料一天更新
    一次已經足夠即時）。"""
    if c := _cache_get("valuation_map"):
        return c
    merged = {}
    merged.update(_fetch_twse_valuation())
    merged.update(_fetch_tpex_valuation())
    return _cache_set("valuation_map", merged)


# ════════════════════════════════════════════════
# 融資融券餘額（籌碼面風險：個股層級）
# ════════════════════════════════════════════════
# ★ 新增：2026-09-27——使用者要求補上「融券餘額 + 當沖比例（籌碼面風險）」。
# 個股融資融券餘額兩個市場都已實測確認欄位名稱：
#   TWSE: openapi.twse.com.tw/v1/exchangeReport/MI_MARGN（中文欄位）
#         股票代號/股票名稱/融資今日餘額/融資限額/融券今日餘額/融券前日餘額/
#         融券限額/資券互抵/註記 等
#   TPEx: www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance（英文欄位）
#         SecuritiesCompanyCode/CompanyName/MarginPurchaseBalance/
#         MarginPurchaseQuota/ShortSaleBalance/ShortSaleBalancePreviousDay/
#         ShortSaleQuota 等
# 「當沖比例」目前只確認到市場層級的彙總資料可用（TPEx
# tpex_intraday_trading_statistics：全市場當沖成交值占大盤比重），TWSE
# 對應的 exchangeReport/TWTB4U 官方 OpenAPI 實測只有「當日可當沖標的清單」
# 四個欄位（Date/Code/Name/Suspension），沒有個股當沖比重數字；TWSE 個股
# 層級的當沖比重目前沒找到官方 OpenAPI 資料源（見檔案最後 TODO），所以這裡
# 先做「個股融券餘額」這個確定可拿到的籌碼面風險訊號，當沖比例先只做
# TPEx 市場層級的 overlay，個股層級當沖比重留待之後找到資料源再補。
def _fetch_twse_margin_short() -> Dict[str, Dict]:
    """上市個股融資融券餘額，TWSE OpenAPI。"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_margin_short: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code = (row.get("股票代號") or "").strip()
            if not code:
                continue
            short_today = _f(row.get("融券今日餘額"))
            short_prev  = _f(row.get("融券前日餘額"))
            short_quota = _f(row.get("融券限額"))
            result[code] = {
                "short_balance": short_today,
                "short_balance_chg": (short_today - short_prev)
                                      if (short_today is not None and short_prev is not None) else None,
                "short_utilization_pct": round(short_today / short_quota * 100, 2)
                                          if (short_today and short_quota) else None,
                "margin_balance": _f(row.get("融資今日餘額")),
                "margin_quota": _f(row.get("融資限額")),
                "offsetting": _f(row.get("資券互抵")),
                "source": "twse_openapi",
            }
        logger.info(f"融資融券餘額（上市）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_margin_short: {e}")
        return {}


def _fetch_tpex_margin_short() -> Dict[str, Dict]:
    """上櫃個股融資融券餘額，TPEx OpenAPI。"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_tpex_margin_short: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code = (row.get("SecuritiesCompanyCode") or "").strip()
            if not code:
                continue
            short_today = _f(row.get("ShortSaleBalance"))
            short_prev  = _f(row.get("ShortSaleBalancePreviousDay"))
            short_quota = _f(row.get("ShortSaleQuota"))
            result[code] = {
                "short_balance": short_today,
                "short_balance_chg": (short_today - short_prev)
                                      if (short_today is not None and short_prev is not None) else None,
                "short_utilization_pct": _f(row.get("ShortSaleUtilizationRate")),
                "margin_balance": _f(row.get("MarginPurchaseBalance")),
                "margin_quota": _f(row.get("MarginPurchaseQuota")),
                "offsetting": _f(row.get("Offsetting")),
                "source": "tpex_openapi",
            }
        logger.info(f"融資融券餘額（上櫃）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_tpex_margin_short: {e}")
        return {}


def fetch_margin_short_map() -> Dict[str, Dict]:
    """回傳 {代號: {short_balance, short_balance_chg, short_utilization_pct,
    margin_balance, margin_quota, offsetting, source}}，涵蓋上市+上櫃。
    short_balance_chg > 0 代表融券餘額比前一交易日增加（放空/避險部位增加，
    籌碼面風險上升訊號之一），short_utilization_pct 是融券使用率（今日餘額
    佔限額比例），數字愈高代表愈接近該股融券額度上限。"""
    if c := _cache_get("margin_short_map"):
        return c
    merged = {}
    merged.update(_fetch_twse_margin_short())
    merged.update(_fetch_tpex_margin_short())
    return _cache_set("margin_short_map", merged)


def fetch_market_daytrading_overlay() -> Dict:
    """回傳全市場（目前僅 TPEx 有官方 OpenAPI 資料源，見上方 TODO）當日沖銷
    成交值占大盤比重的最新一筆，作為籌碼面風險的大盤層級 overlay 使用；
    個股層級當沖比重目前沒有資料源，回傳的是市場整體數字，不是個股專屬。
    抓不到就回傳空字典（優雅降級，不影響任何既有訊號邏輯）。"""
    if c := _cache_get("market_daytrading_overlay"):
        return c
    url = "https://www.tpex.org.tw/openapi/v1/tpex_intraday_trading_statistics"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            rows = r.json()
            if rows:
                last = rows[-1]
                result = {
                    "date": last.get("Date", ""),
                    "volume_pct_of_market": _f(str(last.get("DayTradingVolumeOfTheMarket", "")).replace("%", "")),
                    "buy_value_pct_of_market": _f(str(last.get("DayTradingValueOfBuyOfTheMarket", "")).replace("%", "")),
                    "sell_value_pct_of_market": _f(str(last.get("DayTradingValueOfSellsOfTheMarket", "")).replace("%", "")),
                    "scope": "tpex_market_wide",
                    "source": "tpex_openapi",
                }
                return _cache_set("market_daytrading_overlay", result)
    except Exception as e:
        logger.warning(f"fetch_market_daytrading_overlay: {e}")
    return {}


# ════════════════════════════════════════════════
# 獲利品質（毛利率/營業利益率，季報，目前僅上市「一般業」）
# ════════════════════════════════════════════════
# ★ 新增：2026-09-28——使用者要求把「獲利品質」接進評分。TWSE OpenAPI 的
# opendata/t187ap06_L_ci（上市「一般業」公司綜合損益表）欄位已實測確認：
# 公司代號/公司名稱/年度/季別/營業收入/營業成本/營業毛利（毛損）淨額/
# 營業利益（損失）/本期淨利（淨損）等。
#
# ★ 修正：2026-09-28（使用者要求「金融業抓不到資料要去別的地方找」）——原本
# 只做一般業，銀行/證券期貨/保險/金控這幾個子分類（basi/bd/fh/ins）的欄位
# 名稱已經逐一實測確認（見下方各自的 fetch 函式），全部補上；只有「異業」
# （mim）還沒做，因為它是個雜項分類，成分股跟台灣50沒有重疊，優先度較低。
# 銀行/證券期貨/金控這幾類商業模式本來就沒有「營業成本/毛利」的概念（銀行
# 的獲利來源是利差不是賣東西的毛利），所以這幾類的 gross_margin_pct 會是
# None——這是「這個概念不適用」，不是「沒抓到資料」，呼叫端要分清楚兩者。
# operating_margin_pct 則用各行業結構裡最接近「營業利益」的科目：
#   ci/ins：營業利益（損失）
#   bd（證券期貨業）：營業利益
#   fh（金控業）：繼續營業單位稅前損益（金控業損益表沒有單獨的「營業利益」
#     科目，稅前損益是最接近的替代指標，嚴格說是「稅前利潤率」不是「營益
#     率」，這裡沿用同一個欄位名稱是為了讓 scanner.py 的評分邏輯不用為
#     金融股另外寫一套規則，但數字的實際意義跟一般業的營益率不完全一樣，
#     這點寫在 industry_type 欄位讓呼叫端可以識別）
#   basi（銀行業）：繼續營業單位稅前淨利（淨損）
_QUALITY_CACHE_TTL_SEC = 3600 * 24  # 季報一季才更新一次，24小時快取足夠保守

def _parse_common(row: Dict) -> tuple:
    code = (row.get("公司代號") or "").strip()
    year = (row.get("年度") or "").strip()
    quarter = (row.get("季別") or "").strip()
    period = f"{year}Q{quarter}" if (year and quarter) else ""
    return code, period


def _fetch_twse_income_statement_ci() -> Dict[str, Dict]:
    """上市「一般業」公司最新一期綜合損益表，TWSE OpenAPI。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_income_statement_ci: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code, period = _parse_common(row)
            if not code:
                continue
            revenue = _f(row.get("營業收入"))
            gross = _f(row.get("營業毛利（毛損）淨額"))
            if gross is None:
                gross = _f(row.get("營業毛利（毛損）"))
            op_income = _f(row.get("營業利益（損失）"))
            net_income = _f(row.get("本期淨利（淨損）"))
            result[code] = {
                "period": period, "industry_type": "ci",
                "revenue": revenue, "gross_profit": gross, "operating_income": op_income,
                "net_income": net_income,
                "gross_margin_pct": round(gross / revenue * 100, 2) if (gross is not None and revenue) else None,
                "operating_margin_pct": round(op_income / revenue * 100, 2) if (op_income is not None and revenue) else None,
                "source": "twse_openapi_ci",
            }
        logger.info(f"獲利品質（上市一般業）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_income_statement_ci: {e}")
        return {}


def _fetch_twse_income_statement_ins() -> Dict[str, Dict]:
    """上市「保險業」，欄位結構跟一般業相同（有營業收入/營業成本/營業利益），
    差別只是沒有單獨的「營業毛利」科目，用 revenue-cost 自己算。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ins"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_income_statement_ins: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code, period = _parse_common(row)
            if not code:
                continue
            revenue = _f(row.get("營業收入"))
            cost = _f(row.get("營業成本"))
            gross = (revenue - cost) if (revenue is not None and cost is not None) else None
            op_income = _f(row.get("營業利益（損失）"))
            net_income = _f(row.get("本期淨利（淨損）"))
            result[code] = {
                "period": period, "industry_type": "ins",
                "revenue": revenue, "gross_profit": gross, "operating_income": op_income,
                "net_income": net_income,
                "gross_margin_pct": round(gross / revenue * 100, 2) if (gross is not None and revenue) else None,
                "operating_margin_pct": round(op_income / revenue * 100, 2) if (op_income is not None and revenue) else None,
                "source": "twse_openapi_ins",
            }
        logger.info(f"獲利品質（上市保險業）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_income_statement_ins: {e}")
        return {}


def _fetch_twse_income_statement_bd() -> Dict[str, Dict]:
    """上市「證券期貨業」，沒有毛利概念（收益-支出及費用=營業利益）。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_bd"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_income_statement_bd: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code, period = _parse_common(row)
            if not code:
                continue
            revenue = _f(row.get("收益"))
            op_income = _f(row.get("營業利益"))
            net_income = _f(row.get("本期淨利（淨損）"))
            result[code] = {
                "period": period, "industry_type": "bd",
                "revenue": revenue, "gross_profit": None, "operating_income": op_income,
                "net_income": net_income,
                "gross_margin_pct": None,  # 證券期貨業沒有「成本/毛利」概念
                "operating_margin_pct": round(op_income / revenue * 100, 2) if (op_income is not None and revenue) else None,
                "source": "twse_openapi_bd",
            }
        logger.info(f"獲利品質（上市證券期貨業）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_income_statement_bd: {e}")
        return {}


def _fetch_twse_income_statement_fh() -> Dict[str, Dict]:
    """上市「金控業」，沒有毛利概念，operating_margin_pct 這裡用「稅前損益率」
    （繼續營業單位稅前損益/利息以外淨收益）替代——金控業損益表沒有單獨的
    營業利益科目，這是最接近的替代指標，嚴格說不是傳統定義的營益率（見
    上方檔案開頭的說明），這個欄位在金控股身上的意義跟一般業不完全一樣。

    ★ 修正：2026-09-28（上線後用台灣50驗證立刻發現的bug）——revenue 分母
    原本用「淨收益」欄位，但實測拿富邦金(2881)的真實原始數字回推發現，
    「淨收益」其實是已經扣掉呆帳費用/保險負債準備變動/營業費用之後的
    「淨額」（數值遠小於稅前損益），拿它當分母會算出稅前損益率302%這種
    荒謬數字。真正該當「總收益」分母的是「利息以外淨收益」這個欄位——
    雖然欄位名稱看起來像「利息以外的收益」，但實測數字等於「利息淨收益+
    其他收益及費損淨額」的加總，也就是金控業扣除各項費用「之前」的總
    收益，這是 TWSE 資料源本身欄位命名容易誤導的地方（銀行業 basi 分類
    沒有這個問題，欄位命名邏輯正常，已用彰銀2801的真實數字驗證過）。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_fh"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_income_statement_fh: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code, period = _parse_common(row)
            if not code:
                continue
            revenue = _f(row.get("利息以外淨收益"))
            pretax = _f(row.get("繼續營業單位稅前損益"))
            net_income = _f(row.get("本期稅後淨利（淨損）"))
            result[code] = {
                "period": period, "industry_type": "fh",
                "revenue": revenue, "gross_profit": None, "operating_income": pretax,
                "net_income": net_income,
                "gross_margin_pct": None,  # 金控業沒有「成本/毛利」概念
                "operating_margin_pct": round(pretax / revenue * 100, 2) if (pretax is not None and revenue) else None,
                "source": "twse_openapi_fh",
            }
        logger.info(f"獲利品質（上市金控業）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_income_statement_fh: {e}")
        return {}


def _fetch_twse_income_statement_basi() -> Dict[str, Dict]:
    """上市「銀行業」，跟金控業同樣沒有毛利概念，revenue 用「利息淨收益+
    利息以外淨損益」相加（這個資料源沒有給一個現成的「總收益」欄位，要
    自己加總），operating_margin_pct 用稅前淨利率替代（理由同金控業）。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_basi"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_income_statement_basi: HTTP {r.status_code}")
            return {}
        result = {}
        for row in r.json():
            code, period = _parse_common(row)
            if not code:
                continue
            interest_net = _f(row.get("利息淨收益"))
            non_interest_net = _f(row.get("利息以外淨損益"))
            revenue = (interest_net + non_interest_net) if (interest_net is not None and non_interest_net is not None) else None
            pretax = _f(row.get("繼續營業單位稅前淨利（淨損）"))
            net_income = _f(row.get("本期淨利（淨損）"))
            result[code] = {
                "period": period, "industry_type": "basi",
                "revenue": revenue, "gross_profit": None, "operating_income": pretax,
                "net_income": net_income,
                "gross_margin_pct": None,  # 銀行業沒有「成本/毛利」概念
                "operating_margin_pct": round(pretax / revenue * 100, 2) if (pretax is not None and revenue) else None,
                "source": "twse_openapi_basi",
            }
        logger.info(f"獲利品質（上市銀行業）：{len(result)} 檔")
        return result
    except Exception as e:
        logger.warning(f"_fetch_twse_income_statement_basi: {e}")
        return {}


def fetch_profitability_quality_map() -> Dict[str, Dict]:
    """回傳 {代號: {period, industry_type, revenue, gross_profit,
    operating_income, net_income, gross_margin_pct, operating_margin_pct,
    source}}，只有「當期累計絕對水準」，不含 gross_margin_chg/
    operating_margin_chg（季度變化量）——這兩個 trend 欄位要另外呼叫
    get_profitability_quality(code) 才會有，見該函式說明。這個函式本身
    不碰資料庫，單純是 TWSE 五個子分類端點的合併+快取包裝（一般業/保險業/
    證券期貨業/金控業/銀行業，異業 mim 還沒做，見檔案最後 TODO）。

    ★ 修正：2026-09-28（上線後立刻發現的效能問題）——原本這裡在拿到全市場
    900+ 檔資料後，會對「每一檔」都做一次資料庫讀+寫（比對/記錄季度快照），
    實測部署後連 /api/state 這種既有的輕量端點都被拖到逾時，這個函式本身
    也是同一批（每次呼叫都對全市場做900+次序列化DB往返，Render上的
    Postgres加上網路延遲，這規模的序列化I/O足以拖到數十秒甚至逾時）。這個
    函式在 scanner.py 的 _filter_and_rank() 裡，每次掃描只需要看「當次候選
    訊號」（通常幾十檔以內）的獲利品質，卻要為了這幾十檔而先對全市場900+
    檔做資料庫寫入，規模完全不成比例——所以改成這裡只做「抓資料+快取」，
    資料庫讀寫（也就是「趨勢比對」）搬到 get_profitability_quality(code)，
    只在呼叫端真的要看某一檔股票時才做，把資料庫I/O從900+次降到呼叫端
    實際需要的幾十次。"""
    if c := _cache_get("profitability_quality_map"):
        return c
    merged = {}
    # 合併順序沒有特別意義（每個分類彼此的代號不會重疊，同一檔股票只會
    # 屬於一個產業分類），這裡依序合併五個已驗證欄位名稱的子分類。
    # ★ 新增：2026-09-28（三態資料品質，item 2）——每個子分類各有幾百檔，
    # 正常情況下子端點抓到的絕對不會是空字典，所以用「這次有沒有抓到任何
    # 一筆」當作該子端點「這次有沒有抓成功」的近似判斷（不修改五個子函式
    # 各自回傳 tuple，避免這次改動範圍過大）。只要五個子端點「至少一個」
    # 這次抓成功，就不算整體 unavailable——个股層級的細緻歸屬（例如金控股
    # 剛好碰到 fh 端點失敗）目前不做，呼叫端只能知道「這次整體資料源健康
    # 與否」，這是刻意的簡化範圍，非完整方案。
    any_ok = False
    for fetch_fn in (
        _fetch_twse_income_statement_ci, _fetch_twse_income_statement_ins,
        _fetch_twse_income_statement_bd, _fetch_twse_income_statement_fh,
        _fetch_twse_income_statement_basi,
    ):
        d = fetch_fn()
        if d:
            any_ok = True
        merged.update(d)
    _cache_set("profitability_quality_fetch_ok", any_ok)
    return _cache_set("profitability_quality_map", merged)


def profitability_quality_fetch_ok() -> bool:
    """★ 新增：2026-09-28（三態資料品質，item 2）——回傳「最近一次
    fetch_profitability_quality_map() 是否至少有一個子分類端點抓成功」。
    False 時，某檔股票在 map 裡查不到，是因為「這次資料源整體抓失敗」
    （unavailable），不是「這檔本來就不在涵蓋範圍」（not_covered，例如
    上櫃或異業分類）；呼叫端（scanner.py）拿這個搭配 q is None 組出
    checked/not_covered/unavailable 三態。還沒呼叫過
    fetch_profitability_quality_map() 之前保守回傳 False。"""
    c = _cache_get("profitability_quality_fetch_ok")
    return bool(c) if c is not None else False


def _derive_single_quarter(cum_now: Dict, cum_prev: Optional[Dict], quarter: int) -> Dict:
    """把「累計數」反推成「單季數」。TWSE 這幾個季報端點的 revenue/gross_
    profit/operating_income 都是「今年至該季為止的累計數」，不是單季數字
    （2026-09-28 使用者拿台灣50比對台積電法說會公布的單季毛利率67.7%，
    系統原本算出來是67.03%，兩者的量級差異就是「累計 vs 單季」造成的——
    revenue攔位是法說會公布單季營收的將近兩倍，符合「上半年累計」）。
    第一季（quarter==1）本身就是單季，不用減；其餘季別要用「這一季累計」
    減去「上一季累計」（cum_prev，必須是同一年度的上一季，呼叫端要保證
    這點），減不出來（cum_prev缺某個欄位）的部分就是 None，不是0。"""
    if quarter == 1 or cum_prev is None:
        return cum_now
    out = {}
    for k in ("revenue", "gross_profit", "operating_income"):
        v_now, v_prev = cum_now.get(k), cum_prev.get(k)
        out[k] = (v_now - v_prev) if (v_now is not None and v_prev is not None) else None
    return out


def _margin_from_single(single: Dict) -> tuple:
    revenue = single.get("revenue")
    gross_margin = (round(single["gross_profit"] / revenue * 100, 2)
                     if (single.get("gross_profit") is not None and revenue) else None)
    op_margin = (round(single["operating_income"] / revenue * 100, 2)
                  if (single.get("operating_income") is not None and revenue) else None)
    return gross_margin, op_margin


def get_profitability_quality(code: str) -> Optional[Dict]:
    """對外接口：只針對「單一檔股票」做資料庫讀寫來算季度趨勢，供 scanner.py
    對候選訊號逐檔呼叫用——絕對不要在全市場迴圈裡呼叫這個函式（見上方
    fetch_profitability_quality_map() 的效能教訓），只給已經篩到候選名單的
    個股用。回傳格式同 fetch_profitability_quality_map() 的單檔內容，外加
    gross_margin_chg/operating_margin_chg——這兩個是「反推出單季數字後」
    跟上一個單季比較的變化量（百分點），不是直接拿累計數的比率相減（見
    _derive_single_quarter() 說明，這是2026-09-28驗證台灣50數據時發現的
    修正，原本是直接拿累計比率相減，Q2 vs Q1還好，Q3 vs Q2這種會被多一季
    累計進去稀釋，不夠嚴謹）。缺歷史資料可比較時兩個 chg 欄位是 None，
    不是0——「沒有資料」和「持平」是兩回事。這檔股票在
    fetch_profitability_quality_map() 裡沒有資料（例如上櫃、異業分類）
    時回傳 None。"""
    info = fetch_profitability_quality_map().get(code)
    if not info:
        return None
    info = dict(info)  # 複製一份，避免修改到共用快取裡的物件
    period = info.get("period", "")
    if not period or "Q" not in period:
        return info
    try:
        year_str, quarter_str = period.split("Q", 1)
        year, quarter = int(year_str), int(quarter_str)
    except (ValueError, IndexError):
        return info
    cum_now = {"revenue": info.get("revenue"), "gross_profit": info.get("gross_profit"),
               "operating_income": info.get("operating_income")}
    try:
        from state_store import store
        # 存這一期的「累計原始數字」，讓下一季呼叫時可以反推單季——
        # gross_margin_pct/operating_margin_pct 這裡存的是累計比率，只是
        # 給人工查資料庫時參考用，真正拿來算 trend 的是 cum_* 三個原始欄位。
        store.save_quarterly_margin(code, period, info.get("gross_margin_pct"), info.get("operating_margin_pct"),
                                     cum_now.get("revenue"), cum_now.get("gross_profit"), cum_now.get("operating_income"))

        prev1 = None if quarter == 1 else store.get_quarterly_margin_snapshot(code, f"{year}Q{quarter - 1}")
        if prev1 is None:
            return info  # 沒有上一季資料可反推單季，當期水準照樣回傳，trend留空

        prev1_cum = {"revenue": prev1.get("cum_revenue"), "gross_profit": prev1.get("cum_gross_profit"),
                     "operating_income": prev1.get("cum_operating_income")}
        single_now = _derive_single_quarter(cum_now, prev1_cum, quarter)

        prev2 = None if quarter <= 2 else store.get_quarterly_margin_snapshot(code, f"{year}Q{quarter - 2}")
        prev2_cum = ({"revenue": prev2.get("cum_revenue"), "gross_profit": prev2.get("cum_gross_profit"),
                      "operating_income": prev2.get("cum_operating_income")} if prev2 else None)
        single_prev = _derive_single_quarter(prev1_cum, prev2_cum, quarter - 1)

        gm_now, om_now = _margin_from_single(single_now)
        gm_prev, om_prev = _margin_from_single(single_prev)
        if gm_now is not None and gm_prev is not None:
            info["gross_margin_chg"] = round(gm_now - gm_prev, 2)
        if om_now is not None and om_prev is not None:
            info["operating_margin_chg"] = round(om_now - om_prev, 2)
        # 附上反推出來的單季水準，跟原本 info 裡的「累計水準」分開放，
        # 呼叫端要展示「這一季真正的單季毛利率」時用這兩個新欄位，不要
        # 誤用 gross_margin_pct/operating_margin_pct（那兩個是累計）。
        info["gross_margin_pct_single_q"] = gm_now
        info["operating_margin_pct_single_q"] = om_now
    except Exception as e:
        logger.warning(f"get_profitability_quality({code}): 季度趨勢比對失敗（不影響當期資料，trend 留空): {e}")
    return info


# ════════════════════════════════════════════════
# 重大訊息公告（紅旗關鍵字，軟性扣分＋顯示，不做硬性排除）
# ════════════════════════════════════════════════
# ★ 新增：2026-09-28——使用者要求接「重大訊息公告」。TWSE OpenAPI 的
# opendata/t187ap04_L（上市公司每日重大訊息）欄位已實測確認：公司代號/
# 公司名稱/發言日期/主旨/說明 等。這個端點只回傳「最近幾天」的公告（實測
# 抓到的是最新一批，官方文件沒明講確切保留天數），不是完整歷史，所以只能
# 當「近期有沒有負面重大訊息」的即時訊號，不能拿來做統計；只做上市公司，
# 上櫃公司對應資料源還沒找到。
_MATERIAL_NEWS_CACHE_TTL_SEC = 3600 * 6  # 重大訊息即時性較高，6小時重抓一次

# 只挑「已發生、方向明確偏負面」的字詞，刻意不做「正面關鍵字加分」——
# 像「股利分派」「董事會決議」「名稱變更」這類中性/正面公告本來就佔多數，
# 重大訊息公告的本質是風險揭露用途，拿來當加分理由容易本末倒置。這份
# 清單一定會有一定比例的誤判（關鍵字出現在無關語境，例如「內部控制」也
# 可能出現在「強化內部控制」這種正面語境），所以底下評分邏輯是小幅扣分
# 而不是直接排除（設計理由見 scanner.py 對應段落）。
MATERIAL_NEWS_NEGATIVE_KEYWORDS = [
    "存款不足", "跳票", "聲請重整", "破產", "下市", "停止買賣", "全額交割",
    "掏空", "淘空", "檢調", "搜索", "起訴", "收押", "重大訴訟", "重大裁罰",
    "財報重編", "更換簽證會計師", "董事長辭職", "總經理辭職", "財務長辭職",
    "內部控制", "重大缺失", "存貨跌價", "重大虧損", "停工", "資產減損",
    "信用評等調降",
]

def _fetch_material_news_map() -> tuple:
    """上市公司每日重大訊息，命中負面關鍵字的才保留。回傳 (result, ok)——
    ★ 新增：2026-09-28（三態資料品質，item 2）：ok=False 代表這次抓取本身
    失敗（HTTP錯誤／例外），這時 result 一定是空字典。呼叫端要能分辨
    「抓取失敗、根本沒查」跟「抓到了、確認沒有負面重大訊息」這兩種情況，
    不能讓兩者在下游看起來一樣（都是「這檔不在 result 裡」）。"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_material_news_map: HTTP {r.status_code}")
            return {}, False
        result: Dict[str, List[Dict]] = {}
        for row in r.json():
            code = (row.get("公司代號") or "").strip()
            subject = (row.get("主旨") or "").strip()
            if not code or not subject:
                continue
            if any(kw in subject for kw in MATERIAL_NEWS_NEGATIVE_KEYWORDS):
                result.setdefault(code, []).append({
                    "date": row.get("發言日期", ""),
                    "subject": subject,
                })
        logger.info(f"重大訊息（負面關鍵字命中）：{len(result)} 檔")
        return result, True
    except Exception as e:
        logger.warning(f"_fetch_material_news_map: {e}")
        return {}, False


def fetch_all_news_rows() -> List[Dict]:
    """上市公司「全部」重大訊息（不只負面關鍵字），10 分鐘快取。
    供「我的持股／觀察清單／有效訊號」的即時公告提醒使用。"""
    if (c := _cache_get("all_news_rows")) is not None:
        return c
    rows = []
    try:
        r = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", headers=HEADERS, timeout=15)
        if r.status_code == 200:
            for row in r.json():
                code = (row.get("公司代號") or "").strip()
                subject = (row.get("主旨") or "").strip()
                if not code or not subject:
                    continue
                rows.append({"code": code, "name": (row.get("公司名稱") or "").strip(),
                             "date": row.get("發言日期", ""), "time": row.get("發言時間", ""),
                             "subject": subject,
                             "negative": any(kw in subject for kw in MATERIAL_NEWS_NEGATIVE_KEYWORDS)})
        else:
            logger.warning(f"fetch_all_news_rows: HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"fetch_all_news_rows: {e}")
    return _cache_set("all_news_rows", rows)


def fetch_material_news_risk_map() -> Dict[str, List[Dict]]:
    """對外接口，加上快取。回傳 {代號: [{date, subject}, ...]}，只涵蓋
    命中負面關鍵字的上市公司；沒命中或找不到資料的股票不會是這個 map
    的 key（呼叫端用 .get(code) 判斷「近期沒有負面重大訊息」）。這個函式
    本身涵蓋範圍只有上市（t187ap04_L 沒有上櫃對應端點），呼叫端要判斷
    「上櫃、本來就不在涵蓋範圍」請用 code 的市場別（例如月營收 map 的
    source 欄位），不要用這個函式的回傳值判斷涵蓋範圍。"""
    if c := _cache_get("material_news_risk_map"):
        return c
    result, ok = _fetch_material_news_map()
    _cache_set("material_news_fetch_ok", ok)
    return _cache_set("material_news_risk_map", result)


def material_news_fetch_ok() -> bool:
    """★ 新增：2026-09-28（三態資料品質，item 2）——回傳「最近一次
    fetch_material_news_risk_map() 抓取是否成功」。False 時，代表當次
    「沒命中」是因為根本沒抓到資料（fail-open，不是「查過確認乾淨」）；
    呼叫端（scanner.py）拿這個搭配月營收 source 欄位判斷的市場別，組出
    checked/not_covered/unavailable 三態。還沒呼叫過
    fetch_material_news_risk_map() 之前保守回傳 False（視為「不確定」，
    跟 fetch_market_regime() 保守失敗的設計原則一致）。"""
    c = _cache_get("material_news_fetch_ok")
    return bool(c) if c is not None else False


# ── TODO（下一步，尚未實作，誠實列出目前的覆蓋率限制）──
# 1) 個股層級當沖比重（TWSE+TPEx）：官方 OpenAPI 目前確認只有 TWTB4U
#    （當日可當沖標的清單，沒有比重數字）跟 TPEx 市場層級彙總
#    （tpex_intraday_trading_statistics），2026-09-28 重新搜尋過一輪
#    （含 TWTBAU1/TWTBAU2 等關鍵字比對）依然沒找到官方 OpenAPI 的個股
#    當沖比重端點，暫時判定為「免費官方資料源不存在」，不是還沒找而已；
#    如果之後要做，大概率要走付費資料商或自行爬證交所非API網頁報表。
# 2) 獲利品質目前涵蓋上市「一般業/保險業/證券期貨業/金控業/銀行業」（見上方
#    fetch_profitability_quality_map 說明，2026-09-28 使用者反映金融業
#    抓不到資料後補上）。異業分類（mim）跟全部上櫃公司還沒有實作，這些
#    公司在 quality_map 裡沒有 key，不代表獲利沒問題。金控業/銀行業沒有
#    「毛利」概念，gross_margin_pct 固定是 None（不是資料缺漏，是這個概念
#    不適用），operating_margin_pct 用「稅前損益率」替代，跟一般業的
#    「營業利益率」定義不完全一樣，這點務必不要混為一談。
# 3) 重大訊息公告只做上市公司、只做關鍵字比對（不是語意判斷），一定有
#    一定比例的誤判，評分邏輯刻意設計成小幅扣分不是硬性排除（見
#    scanner.py 對應段落的設計理由）。
