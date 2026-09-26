"""
data_fetcher.py v2.2
修正：
★ TWII 改用 TWSE 官方 API（不依賴 yfinance ticker）
★ TPEX 加 SSL verify=False
★ 黃金改用 GC=F + CoinGecko 備援
★ yfinance download 格式修正
★ 快取 5 分鐘更新
"""
import time, gc, logging, requests, warnings, threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

warnings.filterwarnings("ignore", message=".*SSL.*")
logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False

import os
from config import TIMEFRAMES, SYSTEM, CIRCUIT_BREAKER as CB, TW_MARKET_HOLIDAYS

FUBON_API_KEY = os.getenv("FUBON_API_KEY", "")
FUGLE_API_KEY = os.getenv("FUGLE_API_KEY", "")
FUBON_PUBLIC_BASE = "https://api.fugle.tw/marketdata/v1.0"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ★ 新增：2026-08-31——富果（富邦子公司）行情 API，用來取代常被雲端主機 IP
# 擋掉的 TWSE/TPEX 官方網頁爬蟲。這是需要金鑰的商用 API，走的是完全不同的
# 網路路徑/驗證方式，不會有「Render 雲端 IP 被反爬蟲擋掉」這種問題。
# 金鑰已確認在 Render 環境變數設定且有效（/api/diagnostics 驗證過）。
_fugle_index_discovery_done = False

def _fugle_discover_indices() -> None:
    """
    ★ 除錯用：2026-08-31——第一版猜測的指數代碼「TAIEX」「TPEx」被富果 API
    回 404（代碼不存在），只有 /stock/intraday/quote/{symbol} 這個端點本身是對的，
    symbolId 猜錯了。這裡呼叫富果的 tickers 列表端點（?type=INDEX）各查一次
    上市(TSE)/上櫃(OTC) 的指數清單，把回傳的真實 symbolId 印進 log，
    只在程序啟動後執行一次，用來從正式環境紀錄找出正確代碼，
    找到後應該把結果直接寫死進 _fetch_fugle_index 的呼叫端，並移除這段除錯碼。
    """
    global _fugle_index_discovery_done
    if _fugle_index_discovery_done or not FUGLE_API_KEY:
        return
    _fugle_index_discovery_done = True
    # market=OTC 查回 0 筆，代表富果的「指數」清單不是用股票市場別(TSE/OTC)分類，
    # 這裡改成：market=TSE 撈全部 185 筆，用關鍵字「櫃」「上櫃」「OTC」篩出候選項；
    # 同時也試著完全不帶 market 參數，看是否能撈到更完整/不同的清單。
    for label, params in (("TSE", {"market": "TSE", "type": "INDEX"}),
                           ("OTC", {"market": "OTC", "type": "INDEX"}),
                           ("no_market", {"type": "INDEX"})):
        try:
            r = requests.get(
                f"{FUBON_PUBLIC_BASE}/stock/intraday/tickers",
                headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
                params=params,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                matches = [{"symbol": it.get("symbol"), "name": it.get("name")}
                           for it in data if any(k in (it.get("name") or "") for k in ("櫃", "OTC", "上櫃"))]
                logger.info(f"Fugle 指數清單 {label}: 共{len(data)}筆，含「櫃」關鍵字={matches}")
            else:
                logger.warning(f"Fugle 指數清單 {label}: HTTP {r.status_code} - {r.text[:200]}")
        except Exception as e:
            logger.warning(f"Fugle 指數清單 {label}: {e}")


def _fetch_fugle_index(symbol: str) -> Optional[Dict]:
    """富果 API 抓單一指數（TAIEX=加權指數 / TPEx=上櫃指數）"""
    if not FUGLE_API_KEY:
        return None
    try:
        r = requests.get(
            f"{FUBON_PUBLIC_BASE}/stock/intraday/quote/{symbol}",
            headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            d  = r.json()
            p  = float(d.get("closePrice", 0) or d.get("lastPrice", 0) or 0)
            pv = float(d.get("previousClose", 0) or 0)
            if p > 0:
                return {"price": round(p,2), "prev": round(pv,2) if pv else round(p,2),
                        "chg": round((p-pv)/pv*100,2) if pv else 0,
                        "chg_pt": round(p-pv,2) if pv else 0,
                        "source": "fugle"}
            logger.warning(f"Fugle {symbol}: HTTP 200 但無有效價格欄位 - {r.text[:200]}")
        else:
            logger.warning(f"Fugle {symbol}: HTTP {r.status_code} - {r.text[:200]}")
            _fugle_discover_indices()
    except Exception as e:
        logger.warning(f"Fugle {symbol}: {e}")
    return None

# ══ 快取 ══
_cache: Dict = {}
# ★ 修正：2026-09-03——這個記憶體內快取字典原本沒有任何容量上限，也沒有主動
# 清除機制：get 只是「過期就當沒快取」，過期的項目本身仍然留在 dict 裡佔記憶體，
# 從來不會被移除。fetch_ohlcv() 每一檔股票、每個時間週期都會存一筆（key 是
# f"ohlcv_{ticker}_{tf_key}"），TTL 又設到 3600 秒（見 config.SYSTEM["cache_ttl_sec"]）。
# 全市場單次掃描就有 1000+ 檔，且掃描期間常常會手動重複觸發測試，導致這個字典
# 在一小時內持續疊加、從未被清空。Render 這個方案只有 512MB 記憶體，實測發現
# 掃描進行到一半（batch 12/18）時 Render 平台自己把 instance 重啟了（沒有對應的
# 新 deploy），高度懷疑就是這個無上限的記憶體內快取把常駐記憶體撐爆、被平台判定
# 記憶體超限而強制重啟，導致掃描被腰斬、當次訊號完全無法推播到 Telegram。
# 這裡加上簡單的容量上限（超過就砍掉最舊的項目），並且提供 _cache_clear_all()
# 讓每次全市場掃描開始前主動清空一次，避免同一小時內重複手動觸發掃描時繼續疊加。
_CACHE_MAX_ENTRIES = 400
# ★ 修正：2026-09-03——scanner.py 把單檔序列掃描改成同一批次內最多 4 檔並行後，
# 這個模組層級的 _cache dict 會被多個執行緒同時讀寫。單純的 `_cache[key] = ...`
# 賦值在 CPython 的 GIL 保護下不會壞掉，但下面的容量上限清除邏輯（排序、逐一
# pop）不是原子操作，多個執行緒同時觸發清除時可能互相干擾（不會 crash，因為
# pop 有預設值，但可能清得比預期多或少）。加一個鎖只包住清除這段，平常寫入
# 不用等鎖，只有真的超過上限時才需要互斥。
_cache_lock = threading.Lock()

def _cache_get(key, ttl):
    e = _cache.get(key)
    return e["data"] if e and time.time()-e["ts"]<ttl else None

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}
    if len(_cache) > _CACHE_MAX_ENTRIES:
        with _cache_lock:
            if len(_cache) > _CACHE_MAX_ENTRIES:
                oldest = sorted(_cache.keys(), key=lambda k: _cache[k]["ts"])[: len(_cache) - _CACHE_MAX_ENTRIES]
                for k in oldest:
                    _cache.pop(k, None)
    return data

def _cache_clear(key):
    _cache.pop(key, None)

def _cache_clear_all():
    """在每次全市場掃描開始前呼叫，避免記憶體內快取跨多次掃描無限累積。"""
    n = len(_cache)
    _cache.clear()
    # ★ 新增：2026-09-18——單獨清空 _cache 字典本身省下的記憶體有限（字典
    # 上限只有 400 筆），真正的風險是掃描期間大量 yfinance/pandas DataFrame
    # 物件產生的 CPython 記憶體碎片化（詳見 app.py job_pre_scan_restart() 的
    # 說明，那才是治本作法）。這裡加一次 gc.collect() 純粹是低成本的順手優化
    # ——能回收掉已經沒有任何參照、但還沒被 CPython 世代回收器排到的物件，
    # 不會讓 glibc 把記憶體還給作業系統，所以不能單靠這個解決 OOM 問題。
    gc.collect()
    if n:
        logger.info(f"記憶體快取已清空（原有 {n} 筆，釋放記憶體）")

# ════════════════════════════════════════════════
# 台股加權指數（TWSE 官方 API 為主）
# ════════════════════════════════════════════════
def _fetch_twii() -> Optional[Dict]:
    """TWSE 官方 API 抓加權指數（不依賴 yfinance）"""

    # ★ 修正：2026-08-31——加權指數合理值域原本寫死 5000~30000，
    # 但台股大盤已漲破 45000 點，導致三個來源全部被這道檢查擋掉，
    # 前端才會一直顯示「—」。放寬為 3000~150000 並留大量緩衝空間。
    TWII_MIN, TWII_MAX = 3000, 150000

    # 方法零：富果 API（TAIEX，需金鑰，走商用 API 不受雲端 IP 反爬蟲限制，優先嘗試）
    # ★ 修正：2026-08-31——symbolId 原本猜 "TAIEX" 被富果 API 回 404，
    # 改用富果自家網站確認過的正確代碼 IX0001（發行量加權股價指數）。
    fg = _fetch_fugle_index("IX0001")
    if fg and TWII_MIN < fg["price"] < TWII_MAX:
        logger.info(f"TWII 富果: {fg['price']:.0f}（{fg['chg']:+.2f}%）")
        fg["source"] = "fugle"
        return fg

    # 方法一：TWSE 大盤指數歷史
    try:
        today = datetime.now().strftime("%Y%m%d")
        url   = f"https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={today}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d    = r.json()
            rows = d.get("data", [])
            if rows and len(rows) >= 2:
                last  = rows[-1];   prev = rows[-2]
                p  = float(str(last[-1]).replace(",", ""))
                pv = float(str(prev[-1]).replace(",", ""))
                if TWII_MIN < p < TWII_MAX:
                    logger.info(f"TWII TWSE: {p:.0f}（{(p-pv)/pv*100:+.2f}%）")
                    return {"price": round(p,2), "prev": round(pv,2),
                            "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2),
                            "source": "twse_official"}
    except Exception as e:
        logger.warning(f"TWII TWSE method1: {e}")

    # 方法二：TWSE 每日收盤指數
    try:
        url = "https://www.twse.com.tw/exchangeReport/FMNAV?response=json"
        r   = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d    = r.json()
            rows = d.get("data", [])
            if rows:
                row = rows[-1]
                p   = float(str(row[1]).replace(",", ""))
                if TWII_MIN < p < TWII_MAX:
                    return {"price": round(p,2), "prev": round(p,2),
                            "chg": 0, "chg_pt": 0, "source": "twse_fmnav"}
    except Exception as e:
        logger.warning(f"TWII TWSE method2: {e}")

    # 方法三：yfinance 多個 period 嘗試
    if YFINANCE_OK:
        for period in ["3mo", "6mo", "1y"]:
            try:
                ticker = yf.Ticker("^TWII")
                h = ticker.history(period=period, interval="1d")
                if h is not None and not h.empty and len(h) >= 2:
                    close_col = h["Close"]
                    p  = float(close_col.iloc[-1])
                    pv = float(close_col.iloc[-2])
                    if TWII_MIN < p < TWII_MAX:
                        logger.info(f"TWII yfinance({period}): {p:.0f}")
                        return {"price": round(p,2), "prev": round(pv,2),
                                "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2),
                                "source": f"yfinance_{period}"}
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"TWII yfinance {period}: {e}")

    logger.error("TWII 所有來源均失敗，使用預設值")
    return {"price": 0, "prev": 0, "chg": 0, "chg_pt": 0, "source": "error"}


def _fetch_tpex() -> Optional[Dict]:
    """TPEX 官方 API 抓上櫃指數"""
    # ★ 修正：2026-08-31——完整除錯記錄（避免以後重複走冤枉路）：
    #   1) 一開始以為是「Render 雲端 IP 被反爬蟲擋掉」，補了瀏覽器標頭、
    #      session cookie、curl_cffi 偽裝 Chrome TLS 指紋，全部失敗且錯誤
    #      訊息一模一樣。
    #   2) 印出實際回應內容才發現：舊網址 market_summary/summary_result.php
    #      其實回傳的是 TPEx 自己的「404 找不到頁面」（標題「404 - 證券
    #      櫃檯買賣中心」），代表這個舊網址已經失效/改版移除，跟雲端 IP
    #      完全無關；改用新路徑 aftertrading/index_summary/summary.php
    #      （market_summary 已改名 index_summary）後，拿到的是標題正確
    #      （「上櫃股價指數收盤行情」）的真實頁面 HTML，但 o=json 參數
    #      沒有效果、還是整包 HTML。
    #   3) 進一步在這個真實頁面的 <script src> 清單裡看到
    #      "/cdn-cgi/challenge-platform/scripts/jsd/main.js"——這是
    #      Cloudflare 的 bot 防護 JS 驗證腳本。代表 TPEx 官網（至少這個
    #      查詢功能的資料層）架在 Cloudflare 之後，需要瀏覽器實際執行一段
    #      JS 驗證流程才能拿到真正資料，不是單純標頭/cookie/TLS 指紋能繞過
    #      的——這才是問題真正的根源，而不是「雲端機房 IP 被擋」。要在程式
    #      層面正面突破，需要跑一個真的能執行 JS 的無頭瀏覽器（例如
    #      Playwright）幫忙拿到 Cloudflare 通過後的資料，這對一個每 5
    #      分鐘跑一次、跑在 Render 0.5c/512MB 方案上的排程服務來說成本偏
    #      高（啟動瀏覽器的記憶體/時間開銷），所以先保留 curl_cffi 這個
    #      「萬一哪天 Cloudflare 設定改變、剛好又能過」的低成本嘗試，抓不到
    #      就直接退到 yfinance 保底來源，不再無限期原地繞。
    tpex_headers = {
        **HEADERS,
        "Referer": "https://www.tpex.org.tw/web/stock/aftertrading/index_summary/summary.php",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    today = datetime.now().strftime("%Y%m%d")
    date_param = f"{today[:4]}/{today[4:6]}/{today[6:]}"
    json_url = f"https://www.tpex.org.tw/web/stock/aftertrading/index_summary/summary.php?l=zh-tw&d={date_param}&o=json"

    def _parse_tpex_json(text: str, source: str) -> Optional[Dict]:
        d = __import__("json").loads(text)
        items = d.get("aaData", [])
        if items:
            row = items[0]
            p = float(str(row[1]).replace(",", "")) if len(row) > 1 else 0
            if 50 < p < 5000:
                return {"price": round(p, 2), "prev": round(p, 2),
                        "chg": 0, "chg_pt": 0, "source": source}
        return None

    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(json_url, headers=tpex_headers, timeout=10, impersonate="chrome", verify=False)
        body = r.text.strip()
        if r.status_code == 200 and body:
            try:
                result = _parse_tpex_json(body, "tpex_official_cffi")
                if result:
                    logger.info(f"TPEX 官方: {result['price']}")
                    return result
            except Exception:
                pass  # 已知會拿到 Cloudflare 擋下的 HTML shell，非 JSON，靜默跳過即可
    except Exception as e:
        logger.warning(f"TPEX official: {e}")

    # 方法三：yfinance 備用 —— ★ 修正：2026-08-31 移除確認不存在的 "^TPEX"
    # （Yahoo 回 404 Quote not found），只保留 "^TWOII" 嘗試。
    # 注意：已知 ^TWOII 跟官方櫃買指數有約 5~6% 落差（例如 2026-08-28 官方
    # 收盤 402.83，^TWOII 同期只有 389.41），只是最後一道保底、不完全準確。
    if YFINANCE_OK:
        for sym in ["^TWOII"]:
            try:
                h = yf.Ticker(sym).history(period="3mo", interval="1d")
                if h is not None and not h.empty and len(h) >= 2:
                    p  = float(h["Close"].iloc[-1])
                    pv = float(h["Close"].iloc[-2])
                    if 50 < p < 5000:
                        return {"price": round(p,2), "prev": round(pv,2),
                                "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2),
                                "source": f"yfinance_{sym}_approx"}
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"TPEX yfinance {sym}: {e}")
    return None


# ════════════════════════════════════════════════
# VIX / DXY / 美股（強制不快取）
# ════════════════════════════════════════════════
def _fetch_yf_single(sym: str, valid_range: tuple = None) -> Optional[Dict]:
    """通用 yfinance 單一指數抓取"""
    if not YFINANCE_OK:
        return None
    for period in ["5d", "1mo"]:
        try:
            h = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=True)
            if h is None or h.empty or len(h) < 2:
                continue
            close = h["Close"]
            p  = float(close.iloc[-1])
            pv = float(close.iloc[-2])
            if valid_range and not (valid_range[0] < p < valid_range[1]):
                continue
            return {"price": round(p,2), "prev": round(pv,2),
                    "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2),
                    "source": "yfinance"}
        except Exception as e:
            logger.warning(f"{sym} {period}: {e}")
        time.sleep(0.2)
    return None


# ════════════════════════════════════════════════
# BTC 價格
# ════════════════════════════════════════════════
def _fetch_btc() -> Optional[Dict]:
    # CoinGecko（免費）
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids":"bitcoin","vs_currencies":"usd","include_24hr_change":"true"},
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200:
            d   = r.json().get("bitcoin", {})
            p   = float(d.get("usd", 0))
            chg = float(d.get("usd_24h_change", 0) or 0)
            if p > 1000:
                return {"price": round(p,0), "chg": round(chg,2), "source": "coingecko"}
    except Exception as e:
        logger.warning(f"BTC coingecko: {e}")

    # yfinance 備用
    result = _fetch_yf_single("BTC-USD", (1000, 10000000))
    return result


# ════════════════════════════════════════════════
# 黃金價格（GC=F 期貨 + 修正倍數）
# ════════════════════════════════════════════════
def _fetch_gold() -> Optional[Dict]:
    # GC=F（COMEX 黃金期貨，最可靠）
    result = _fetch_yf_single("GC=F", (1500, 5000))
    if result:
        result["source"] = "yfinance_GC=F"
        return result

    # CoinGecko 黃金
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids":"gold","vs_currencies":"usd","include_24hr_change":"true"},
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200:
            d = r.json().get("gold", {})
            p = float(d.get("usd", 0))
            if p > 1000:
                return {"price": round(p,2), "chg": float(d.get("usd_24h_change",0) or 0),
                        "source": "coingecko"}
    except Exception as e:
        logger.warning(f"Gold coingecko: {e}")
    return None


# ════════════════════════════════════════════════
# 大盤指數主函數
# ════════════════════════════════════════════════
def fetch_market_index() -> Dict:
    if c := _cache_get("market_index", 300):
        return c

    result = {}

    # 1. 台股加權指數（TWSE 官方 API 為主）
    result["twii"] = _fetch_twii()

    # 2. 上櫃指數
    # ★ 修正：2026-09-26（稽核發現）——原本兩層備援（TPEx官方cffi/yfinance
    # ^TWOII）都失敗時，這裡完全不寫入 "tpex" 這個 key，導致 /api/state 裡
    # 這個指標整個消失、沒有任何「取不到」的訊號，跟 twii 抓不到時仍會回
    # {"price":0,...,"source":"error"} 的做法不一致，下游（前端、
    # /api/diagnostics、任何未來直接讀 idx["tpex"] 的程式）都無法區分
    # 「本來就沒有這個欄位」跟「今天剛好抓不到」。改成失敗時也明確寫入一筆
    # source=="error" 的字典，讓 fail 狀態可見（fail-closed 精神：寧可顯示
    # 明確錯誤，不要靜默消失）。
    tpex = _fetch_tpex()
    result["tpex"] = tpex if tpex else {"price": 0, "prev": 0, "chg": 0, "chg_pt": 0, "source": "error"}

    # 3. VIX（強制重新抓）
    _cache_clear("idx_vix")
    vix = _fetch_yf_single("^VIX", (5, 90))
    if vix:
        result["vix"] = vix

    # 4. DXY（強制重新抓）
    _cache_clear("idx_dxy")
    dxy = _fetch_yf_single("DX-Y.NYB", (70, 130))
    if dxy:
        result["dxy"] = dxy

    # 5. 美股
    sp500 = _fetch_yf_single("^GSPC", (1000, 20000))
    if sp500:
        result["sp500"] = sp500
    nasdaq = _fetch_yf_single("^NDX", (1000, 100000))
    if nasdaq:
        result["nasdaq"] = nasdaq

    # 6. BTC
    btc = _fetch_btc()
    if btc:
        result["btc"] = btc

    # 7. 黃金
    gold = _fetch_gold()
    if gold:
        result["gold"] = gold

    # 8. 大盤狀態
    twii_chg = result.get("twii", {}).get("chg", 0)
    if twii_chg <= CB["twii_drop_stop"]:
        result["market_status"]    = "stop"
        result["market_status_zh"] = f"大盤重挫 {twii_chg:.1f}%，暫停多單"
    elif twii_chg <= CB["twii_drop_caution"]:
        result["market_status"]    = "caution"
        result["market_status_zh"] = f"大盤下跌 {twii_chg:.1f}%，謹慎"
    else:
        result["market_status"]    = "normal"
        result["market_status_zh"] = "大盤正常"

    logger.info(
        f"大盤更新：TWII={result.get('twii',{}).get('price',0):.0f}"
        f"({result.get('twii',{}).get('chg',0):+.2f}%)"
        f" VIX={result.get('vix',{}).get('price','—')}"
        f" DXY={result.get('dxy',{}).get('price','—')}"
        f" BTC={result.get('btc',{}).get('price','—')}"
        f" Gold={result.get('gold',{}).get('price','—')}"
    )
    return _cache_set("market_index", result)


# ════════════════════════════════════════════════
# K線
# ════════════════════════════════════════════════
def _tw_trading_hours_now() -> bool:
    """
    ★ 新增：2026-09-16——判斷現在是不是台股盤中（週一~五 09:00~13:30，
    Asia/Taipei）。用來讓 fetch_ohlcv 的快取在盤中比平常更快過期
    （見下方 _effective_ohlcv_ttl 說明），跟盤中是否有「即時報價」無關
    ——這裡抓的仍然是日K，只是讓「今天這根還在走的日K」在盤中不會被
    卡在舊快取裡長達一小時不更新。
    """
    try:
        now_tw = datetime.now(timezone.utc) + timedelta(hours=8)
        if now_tw.weekday() >= 5:
            return False
        minutes = now_tw.hour * 60 + now_tw.minute
        return 9 * 60 <= minutes <= 13 * 60 + 30
    except Exception:
        return False

def _effective_ohlcv_ttl() -> int:
    # ★ 新增：2026-09-16——稽核發現 fetch_ohlcv 的快取 TTL 固定 3600 秒
    # （config.SYSTEM["cache_ttl_sec"]），不分盤中盤後一律套用同一個值。
    # 本系統目前只在盤後 16:30 跑每日掃描，1小時快取原本問題不大；但
    # /api 系列端點在使用者盤中打開網站查看個股時，也是呼叫同一個
    # fetch_ohlcv，此時如果剛好命中舊快取，看到的「今天」這根日K收盤價
    # 可能是一小時前的價格，跟使用者盤中緊盯行情的預期不符（「資訊更新
    # 速度」問題之一）。這裡把盤中的快取時間縮短到 5 分鐘，盤後/非交易
    # 日則維持原本 1 小時，避免非交易時段做沒必要的重複外部 API 請求。
    return 300 if _tw_trading_hours_now() else SYSTEM["cache_ttl_sec"]

# ★ 新增：2026-09-24——見 state_store.py 的 ohlcv_bars 表說明。fetch_ohlcv()
# 原本每次都對 yfinance 要整段歷史（daily 1年/weekly 5年/hourly 90天），
# 是先前稽核記錄過的最大速度瓶頸（單次全市場掃描約7分鐘）。這裡改成兩層：
# 持久化的 Postgres 快取（跨部署/跨process存活）當底層資料，只在快取缺資料
# 或缺口太大（例如系統停機很久沒更新）時才整段重抓，平常只需要對 yfinance
# 要「快取最新日期之後」的新增K棒（通常1~2根），下載量跟等待時間大幅縮小。
# 模組層級的 _cache（TTL 5分鐘~1小時）維持在最外層，同一個 process 內短時間
# 重複呼叫（例如同一次掃描不同函式都要同一檔的日K）仍然直接命中記憶體、
# 連資料庫都不用查。
_OHLCV_MAX_GAP_DAYS = {"daily": 30, "weekly": 90, "hourly": 10}

def _yf_rows_to_bars(h, tf_key: str = "daily") -> List[Dict]:
    # ★ 修正：2026-09-24——hourly 時框如果跟 daily/weekly 一樣只存日期（不含
    # 時間），同一天內好幾根小時K會全部落在同一個 bar_date，寫進
    # ohlcv_bars（PRIMARY KEY 是 ticker+tf_key+bar_date）就會互相覆蓋掉，
    # 只留當天最後寫入的那一根，等於持久化之後小時線的盤中細節全部消失。
    # 原本純記憶體版本沒有這個問題（dates 只是顯示用的標籤，closes/highs/lows
    # 等數值陣列本身仍保留每一根），但這裡要拿 bar_date 當資料庫主鍵，必須
    # 用足以區分同一天內每一根K棒的字串——hourly 用完整時間戳記，
    # daily/weekly 維持只存日期（跟原本行為一致，也符合這兩種週期本來就是
    # 一天最多一根的特性）。
    use_time = tf_key == "hourly"
    rows = []
    for d, row in h.iterrows():
        try:
            o,hi,lo,c,v = float(row["Open"]),float(row["High"]),float(row["Low"]),float(row["Close"]),float(row["Volume"])
        except Exception:
            continue
        if c <= 0:
            continue
        date_str = d.strftime("%Y-%m-%d %H:%M") if use_time else str(d.date())
        rows.append({"date": date_str, "open": round(o,2), "high": round(hi,2),
                      "low": round(lo,2), "close": round(c,2), "volume": int(v//1000)})
    return rows

def _fetch_ohlcv_incremental(ticker: str, tf_key: str) -> Optional[Dict]:
    from state_store import store
    tf = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])
    last_date = None
    try:
        last_date = store.get_ohlcv_last_date(ticker, tf_key)
    except Exception as e:
        logger.warning(f"_fetch_ohlcv_incremental {ticker}/{tf_key}: 讀取快取最新日期失敗: {e}")

    # ★ 修正：2026-09-24——hourly 快取的 bar_date 現在含時間（"%Y-%m-%d %H:%M"，
    # 見 _yf_rows_to_bars 說明），跟 daily/weekly 純日期格式不同，這裡取日期
    # 部分來算距今天數/當作 yfinance start= 參數，兩種時框都能正確解析。
    last_date_only = (last_date.split(" ")[0] if last_date else None)

    need_full = last_date is None
    if not need_full:
        try:
            gap_days = (datetime.now() - datetime.strptime(last_date_only, "%Y-%m-%d")).days
            if gap_days > _OHLCV_MAX_GAP_DAYS.get(tf_key, 30):
                need_full = True
        except Exception:
            need_full = True

    if not YFINANCE_OK:
        # 沒有 yfinance 可用時，退而求其次直接用資料庫裡現有的快取（可能是舊的，
        # 總比完全沒資料好；真正的新鮮度問題會反映在 get_ohlcv_cache_stats()）。
        need_full = False
        if last_date is None:
            return None
    else:
        try:
            if need_full:
                h = yf.Ticker(ticker).history(period=tf["period"], interval=tf["interval"], auto_adjust=True)
            else:
                # start 用「快取最後一天」當天（含）重抓，蓋掉可能因為盤中提早
                # 抓取而不是定案收盤價/收盤K棒的那部分，其餘全部沿用快取，
                # 不用重抓整段歷史。
                h = yf.Ticker(ticker).history(start=last_date_only, interval=tf["interval"], auto_adjust=True)
            if h is not None and not h.empty:
                new_bars = _yf_rows_to_bars(h, tf_key)
                if new_bars:
                    store.upsert_ohlcv_bars(ticker, tf_key, new_bars)
            elif need_full:
                return None  # 整段重抓卻拿不到任何資料，這檔真的沒資料可用
        except Exception as e:
            logger.warning(f"_fetch_ohlcv_incremental {ticker}/{tf_key}: yfinance 抓取失敗（改用快取既有資料）: {e}")
            if last_date is None:
                return None

    cached = store.get_cached_ohlcv_bars(ticker, tf_key, limit=tf["bars"] + 20)
    cached = [b for b in cached if (b.get("volume") or 0) > 0][-tf["bars"]:]
    if len(cached) < 20:
        return None
    closes  = [b["close"]  for b in cached]
    opens   = [b["open"]   for b in cached]
    highs   = [b["high"]   for b in cached]
    lows    = [b["low"]    for b in cached]
    volumes = [b["volume"] for b in cached]
    dates   = [b["bar_date"] if "bar_date" in b else b["date"] for b in cached]
    if not closes or closes[-1] <= 0:
        return None
    return {
        "ticker": ticker, "tf_key": tf_key, "label": tf["label"],
        "closes": closes, "opens": opens, "highs": highs, "lows": lows,
        "volumes": volumes, "dates": dates,
        "current_price": closes[-1],
        "prev_close":    closes[-2] if len(closes)>1 else closes[-1],
        "change_pct":    round((closes[-1]-closes[-2])/closes[-2]*100,2) if len(closes)>1 else 0,
        "bar_count":     len(closes), "source": "ohlcv_cache",
    }

def fetch_ohlcv(ticker: str, tf_key: str = "daily") -> Optional[Dict]:
    cache_k = f"ohlcv_{ticker}_{tf_key}"
    if c := _cache_get(cache_k, _effective_ohlcv_ttl()):
        return c
    try:
        result = _fetch_ohlcv_incremental(ticker, tf_key)
    except Exception as e:
        logger.warning(f"fetch_ohlcv {ticker} {tf_key}: {e}")
        return None
    if not result:
        return None
    return _cache_set(cache_k, result)

def fetch_all_timeframes(ticker: str) -> Optional[Dict]:
    result = {}
    tf_keys = ["weekly", "daily", "hourly"]
    for i, tf_key in enumerate(tf_keys):
        d = fetch_ohlcv(ticker, tf_key)
        if d: result[tf_key] = d
        # ★ 修正：2026-09-03——原本每個時間週期抓完都 sleep 0.3 秒，包含最後一個
        # （hourly）抓完之後也白白等一次，這個 ticker 的所有工作其實都做完了，
        # 這 0.3 秒純粹浪費（scanner.py 現在已改成多檔並行，每檔的總耗時
        # 直接影響整體吞吐量，不需要的等待要拿掉）。只在還有下一個時間週期
        # 要抓時才需要間隔。
        if i < len(tf_keys) - 1:
            time.sleep(0.3)
    return result if "daily" in result else None

def fetch_batch_current_prices(tickers: List[str]) -> Dict[str, float]:
    if not YFINANCE_OK or not tickers: return {}
    prices = {}
    try:
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i+50]
            data  = yf.download(" ".join(chunk), period="2d", interval="1d",
                                 auto_adjust=True, progress=False)
            if data.empty: continue
            close_data = data["Close"] if "Close" in data else data
            if hasattr(close_data, "columns"):
                for t in chunk:
                    if t in close_data.columns:
                        vals = close_data[t].dropna()
                        if len(vals) > 0:
                            prices[t] = round(float(vals.iloc[-1]), 2)
            time.sleep(0.5)
    except Exception as e:
        logger.error(f"fetch_batch: {e}")
    return prices


# ════════════════════════════════════════════════
# 法人數據
# ════════════════════════════════════════════════
def fetch_institutional_flow(date_str=None) -> Dict:
    cache_k = f"inst_{date_str or 'today'}"
    if c := _cache_get(cache_k, 1800): return c
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200: return {}
        data = r.json()
        if data.get("stat") != "OK": return {}
        result = {}
        # ★ 修正：2026-08-31——T86 實際欄位順序（0-indexed）：
        #   [0]證券代號 [1]證券名稱 [2]外陸資買進股數 [3]外陸資賣出股數
        #   [4]外陸資買賣超股數 [5]外資自營商買進 [6]外資自營商賣出
        #   [7]外資自營商買賣超股數 [8]投信買進股數 [9]投信賣出股數
        #   [10]投信買賣超股數 [11]自營商買賣超股數 ... [18]三大法人買賣超股數
        #   原本 tn 誤取 row[7]（外資自營商買賣超，不是投信）、
        #   tt 誤取 row[11]（自營商買賣超，不是三大法人合計），修正為
        #   正確欄位 row[10]（投信）與 row[18]（三大法人合計）。
        for row in data.get("data", []):
            try:
                code = row[0].strip(); name = row[1].strip()
                def pi(v): return int(v.replace(",","").replace("+","")) if v.strip() not in ("-","") else 0
                fn = pi(row[4]) if len(row)>4 else 0
                tn = pi(row[10]) if len(row)>10 else 0
                tt = pi(row[18]) if len(row)>18 else 0
                result[code] = {"name":name,"foreign_net":fn,"trust_net":tn,"total_net":tt,
                                "signal":"strong_buy" if fn>500 and tn>0 else "buy" if fn>100 else "strong_sell" if fn<-500 else "sell" if fn<-100 else "neutral"}
            except: continue
        logger.info(f"三大法人：{len(result)} 檔")
        return _cache_set(cache_k, result)
    except Exception as e:
        logger.error(f"inst_flow: {e}"); return {}

def fetch_foreign_total_flow() -> Dict:
    if c := _cache_get("foreign_total", 3600): return c
    # ★ 修正：2026-08-31——原本呼叫的 MI_QFIIS?selectType=Daily 其實是
    # 「外資及陸資持股比率統計」（欄位是發行股數、持股比率等，完全沒有
    # 買賣金額），"data" 永遠是空陣列，導致外資動向一直顯示 0億。
    # 改用正確的「三大法人買賣金額統計表」BFI82U，抓「外資及陸資
    # (不含外資自營商)」那一列的買賣差額（新台幣金額）。
    url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200: return {}
        data = r.json()
        if data.get("stat") != "OK": return {}
        rows = data.get("data", [])
        if not rows: return {}
        # 注意：正確那一列的名稱本身就是「外資及陸資(不含外資自營商)」，
        # 字串裡含有「自營商」三個字，不能用排除法；用 startswith 精準比對，
        # 才不會跟另一列「外資自營商」（不同法人）搞混。
        foreign_row = next((row for row in rows if row[0].strip().startswith("外資及陸資")), None)
        if not foreign_row or len(foreign_row) < 4: return {}
        net_buy = int(foreign_row[3].replace(",","").replace("+",""))
        return _cache_set("foreign_total", {
            "date": data.get("date",""), "net_buy_twd": net_buy,
            "net_buy_lots": round(net_buy/1000,0),
            "signal": "strong_buy" if net_buy>50e8 else "buy" if net_buy>0 else "strong_sell" if net_buy<-50e8 else "sell"
        })
    except Exception as e:
        logger.error(f"foreign_total: {e}"); return {}


# ════════════════════════════════════════════════
# 融資餘額增減（CIRCUIT_BREAKER.margin_change_warning 熔斷指標）
# ════════════════════════════════════════════════
# ★ 新增：2026-09-24——使用者要求強化現有總經指標的權重。稽核發現
# config.CIRCUIT_BREAKER["margin_change_warning"] 這個門檻值從一開始就定義
# 在設定檔裡，但整個專案裡從來沒有任何資料抓取或檢查函式真正用到它——是一個
# 「設計了、卻從未接線」的熔斷開關，等於一直沒有真正發揮作用。這裡補上真正
# 的資料來源：TWSE 官方「融資融券餘額」統計，抓全市場融資餘額的日增減幅度，
# 融資餘額急縮通常代表市場情緒轉弱/斷頭賣壓，是波段操作常用的總經觀察指標
# 之一，接進 fetch_market_overview() 讓 risk_manager 可以真正拿到資料做判斷。
def _parse_margin_balance_response(data: Dict):
    """回傳 (prev_bal, today_bal, date_str) 或 None（解析不到）。
    ★ 二次修正：2026-09-24——第一次修正時用的是 WebFetch「摘要」回覆的錯誤
    結構假設（誤以為頂層直接有 fields/creditList），部署後從 production log
    證實 data.get("fields")/data.get("creditList") 在真實回應裡都是
    None/[]，完全沒抓到資料。改用明確要求「逐字列出原始 raw JSON」的
    WebFetch 查證後，確認真正結構其實是巢狀的：
    頂層只有 stat / date / tables（tables 是陣列，每個元素才各自有自己的
    fields 跟 data）。第一個 table（標題含「信用交易統計」）的 data 裡，
    有一列第一欄是「融資金額(仟元)」，欄位依序對應
    fields=["項目","買進","賣出","現金(券)償還","前日餘額","今日餘額"]，
    也就是 idx 4=前日餘額、idx 5=今日餘額。這裡改成正確地走訪
    data["tables"]，在各表自己的 fields/data 裡找目標列。"""
    tables = data.get("tables") or []
    if not tables:
        return None
    def _pi(v):
        try: return int(str(v).replace(",", ""))
        except Exception: return None
    for tbl in tables:
        fields = tbl.get("fields") or []
        rows = tbl.get("data") or []
        if not fields or not rows:
            continue
        def _find_col(keywords, _fields=fields):
            for i, f in enumerate(_fields):
                if all(k in f for k in keywords):
                    return i
            return None
        idx_today = _find_col(["今日餘額"])
        idx_prev  = _find_col(["前日餘額"])
        if idx_today is None:
            continue
        row = next((rw for rw in rows if len(rw) > 0 and "融資金額" in str(rw[0])), None)
        if not row or idx_today >= len(row):
            continue
        today_bal = _pi(row[idx_today])
        prev_bal = _pi(row[idx_prev]) if idx_prev is not None and idx_prev < len(row) else None
        if today_bal is None:
            continue
        return prev_bal, today_bal, data.get("date", "")
    return None


MARGIN_CHANGE_OK_TTL = 3600       # 成功結果快取1小時
MARGIN_CHANGE_FAIL_TTL = 300      # 失敗結果也快取5分鐘，避免無負向快取造成的重試風暴

# ★ 修正：2026-09-26（稽核發現）——原本失敗時回傳空 dict {}，跟 tpex 舊版一樣
# 「靜默消失」，前端/下游沒有任何訊號可以區分「今天剛好是非交易日/假日本來就
# 沒資料」跟「API 真的壞了」。改成失敗時也明確帶 source=="error"/"no_data"，
# 呼應 _fetch_tpex() 的 fail-closed 精神；同時保留 chg_pct 這個 key（值固定
# 為 0），讓既有 `overview.get("margin",{}).get("chg_pct", 0)` 呼叫端行為不變。
def _margin_fail(reason: str) -> Dict:
    return {"chg_pct": 0, "balance": 0, "signal": "no_data", "source": "error",
            "reason": reason, "fetched_at": datetime.now().isoformat(timespec="seconds")}


def fetch_margin_change() -> Dict:
    if c := _cache_get("margin_change", MARGIN_CHANGE_OK_TTL):
        return c
    # 失敗負向快取：用獨立的 key，TTL 較短，讓失敗狀態本身也不會每次呼叫都重打 API，
    # 但又不會像成功結果一樣快取到 1 小時（見稽核報告 s2/s5：無負向快取會讓每次
    # /api/state 被打開都對外部 TWSE API 重複發送請求，且無退避機制）。
    if fc := _cache_get("margin_change_fail", MARGIN_CHANGE_FAIL_TTL):
        return fc
    try:
        today = datetime.now().strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={today}&selectType=ALL"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"margin_change: HTTP {r.status_code}，body前200字={r.text[:200]!r}")
            return _cache_set("margin_change_fail", _margin_fail(f"http_{r.status_code}"))
        data = r.json()
        if data.get("stat") != "OK":
            logger.warning(f"margin_change: stat={data.get('stat')!r}，回應keys={list(data.keys())}")
            # TWSE 對非交易日（週末/假日）通常回 stat!="OK"，這是預期中的「今天沒資料」，
            # 不是故障；reason 用 no_trading_day 跟真正的 API 異常區分開。
            return _cache_set("margin_change_fail", _margin_fail("no_trading_day_or_not_published"))
        parsed = _parse_margin_balance_response(data)
        if not parsed:
            _tables = data.get("tables") or []
            logger.warning(f"margin_change: 解析不到資料，tables數={len(_tables)}，"
                            f"各table標題={[t.get('title') for t in _tables]}")
            return _cache_set("margin_change_fail", _margin_fail("parse_failed"))
        prev_bal, today_bal, date_str = parsed
        chg_pct = round((today_bal - prev_bal) / prev_bal * 100, 2) if prev_bal else 0
        # 順手把每天的餘額存進 meta（供 /api/diagnostics 等處查閱連續趨勢用），
        # 但 chg_pct 本身已經不依賴這份歷史——見上面 _parse_margin_balance_response
        # 的說明，單一天的回應就同時有前日/今日兩個數字，不怕重啟後歸零。
        try:
            from state_store import store
            hist = store.get_meta("margin_balance_history", [])
            hist = [h for h in hist if h.get("date") != date_str] + [{"date": date_str, "balance": today_bal}]
            store.set_meta("margin_balance_history", hist[-10:])
        except Exception:
            pass
        _cache_clear("margin_change_fail")
        return _cache_set("margin_change", {
            "balance": today_bal, "chg_pct": chg_pct,
            "signal": "warning" if chg_pct <= CB.get("margin_change_warning", -5.0) else "normal",
            "source": "twse_official", "fetched_at": datetime.now().isoformat(timespec="seconds"),
        })
    except Exception as e:
        logger.warning(f"margin_change: {e}")
        return _cache_set("margin_change_fail", _margin_fail(f"exception:{e}"))


def backfill_margin_history(days_back: int = 400, progress_cb=None) -> Dict:
    """回填真實歷史融資餘額日增減%（不是模擬資料），存進 state_store 的
    margin_chg_daily_history 表，供 backtester.py 的總經回測直接查表使用。
    ★ 2026-09-24——MI_MARGN 支援歷史 date= 查詢（已用 WebFetch 實測驗證過真的
    能查到過去某天的資料），且單一天回應本身就同時附前一交易日/當日兩個
    餘額，不用逐日往前串接。這裡只嘗試週一到週五的日期（台股週末不開盤，
    直接跳過減少無謂的失敗請求），遇到假日 TWSE 會回傳 stat!=OK，一樣
    優雅跳過、不中斷整個回填流程；已經回填過的日期會自動跳過，可安全重跑。"""
    from state_store import store
    covered = set(store.get_margin_chg_dates_covered())
    today = datetime.now()
    dates = []
    d = today - timedelta(days=1)  # 從昨天開始往回抓，今天盤中的餘額可能還沒定案
    while len(dates) < days_back:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    total = len(dates); done = 0; fetched = 0; skipped = 0; failed = 0; diag_logged = 0
    for dt in dates:
        date_ymd = dt.strftime("%Y%m%d"); date_dash = dt.strftime("%Y-%m-%d")
        done += 1
        if date_dash in covered:
            skipped += 1
            if progress_cb:
                try: progress_cb(done, total, f"[已回填,跳過] {date_dash}")
                except Exception: pass
            continue
        try:
            url = f"https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={date_ymd}&selectType=ALL"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("stat") == "OK":
                    parsed = _parse_margin_balance_response(data)
                    if parsed:
                        prev_bal, today_bal, _ = parsed
                        if prev_bal:
                            chg_pct = round((today_bal - prev_bal) / prev_bal * 100, 2)
                            store.upsert_margin_chg_daily(date_dash, today_bal, chg_pct)
                            fetched += 1
                        elif diag_logged < 3:
                            diag_logged += 1
                            logger.warning(f"backfill_margin_history {date_dash}: 解析成功但沒有前日餘額(prev_bal)可算漲跌%，跳過")
                    elif diag_logged < 3:
                        diag_logged += 1
                        _tables = data.get("tables") or []
                        logger.warning(f"backfill_margin_history {date_dash}: stat=OK但解析不到資料，"
                                        f"tables數={len(_tables)}，各table標題={[t.get('title') for t in _tables]}")
                elif diag_logged < 3:
                    diag_logged += 1
                    logger.warning(f"backfill_margin_history {date_dash}: stat={data.get('stat')!r}（可能是假日，也可能是被擋，先記錄前3筆方便判斷）")
            else:
                if diag_logged < 3:
                    diag_logged += 1
                    logger.warning(f"backfill_margin_history {date_dash}: HTTP {r.status_code}，body前200字={r.text[:200]!r}")
                # stat != OK 通常代表當天不是交易日（假日），優雅跳過不算失敗
        except Exception as e:
            failed += 1
            logger.warning(f"backfill_margin_history {date_dash}: {e}")
        if progress_cb:
            try: progress_cb(done, total, date_dash)
            except Exception: pass
        time.sleep(0.8)
    return {"days_attempted": total, "days_fetched": fetched, "days_skipped_already_covered": skipped,
            "days_failed": failed, "completed_at": datetime.now(timezone.utc).isoformat()}


# ════════════════════════════════════════════════
# 市場總覽
# ════════════════════════════════════════════════
def fetch_market_overview() -> Dict:
    if c := _cache_get("market_overview", 300): return c
    overview = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    overview["index"]         = fetch_market_index()
    overview["foreign"]       = fetch_foreign_total_flow()
    # ★ 移除（稽核發現，2026-09-26）：這裡原本還有一行
    # `overview["institutional"] = fetch_institutional_flow()`。
    # fetch_institutional_flow() 沒帶 date_str 時，抓的是「今天全市場~1700檔
    # 個股」的三大法人買賣超逐股明細（T86），跟 overview["foreign"] 的大盤總
    # 外資買賣超金額（BFI82U）是完全不同的資料形狀，兩者不是同一件事的重複
    # 欄位。全專案搜尋後確認 overview["institutional"] 從未被任何地方讀取
    # （scanner.py 對個股法人資料是各自呼叫 fetch_stock_institutional() 走
    # 自己的快取，跟這裡完全獨立），等於每 5 分鐘 market_overview 快取到期
    # 就白白多打一次全市場 1700 檔的重量級 API 請求，卻沒有任何用途。
    # 個股層級的法人資料查詢入口仍是 fetch_stock_institutional()，未受影響。
    # 見 fetch_margin_change() 說明，把先前「定義了卻從
    # 沒接線」的融資餘額增減熔斷指標，正式接進大盤情緒分數，跟大盤漲跌/VIX/
    # 外資同一層級參與評分，而不是只停在 config 裡的一個死數字。
    overview["margin"]        = fetch_margin_change()
    score = 50
    twii_chg = overview["index"].get("twii",{}).get("chg",0)
    score += 15 if twii_chg>1.5 else 8 if twii_chg>0.5 else 3 if twii_chg>0 else -15 if twii_chg<-1.5 else -8 if twii_chg<-0.5 else -3
    vix_p = overview["index"].get("vix",{}).get("price",20)
    score += 10 if vix_p<15 else 5 if vix_p<20 else -15 if vix_p>30 else -8 if vix_p>25 else 0
    fn = overview.get("foreign",{}).get("net_buy_twd",0)
    score += 15 if fn>50e8 else 8 if fn>10e8 else -15 if fn<-50e8 else -8 if fn<-10e8 else 0
    margin_chg = overview.get("margin",{}).get("chg_pct", 0)
    score += -12 if margin_chg <= CB.get("margin_change_warning", -5.0) else -5 if margin_chg <= -3 else 3 if margin_chg >= 3 else 0
    score = max(0, min(100, score))
    overview["sentiment_score"] = score
    overview["sentiment_zh"]    = "強烈看多" if score>=80 else "偏多" if score>=60 else "中性" if score>=40 else "偏空" if score>=20 else "強烈看空"
    overview["can_trade"]       = overview["index"].get("market_status","normal") != "stop"
    overview["market_status"]   = overview["index"].get("market_status","normal")
    overview["is_trading"]      = _is_trading_session()
    return _cache_set("market_overview", overview)

# ★ 新增：2026-09-25——見 config.py TW_MARKET_HOLIDAYS 的說明。原本
# _is_trading_session()/get_market_session() 都只判斷「是不是週末」，平日
# 國定假日（如中秋、端午）會被誤判成開盤。這裡統一用這個 helper 查詢，
# 找不到當年度清單時退回只用週末判斷並記一次警告 log（用 module-level set
# 記錄已經警告過的年份，避免每次呼叫都洗版 log）。
_holiday_warned_years = set()
def _is_tw_market_holiday(now) -> bool:
    year = now.year
    date_str = now.strftime("%Y-%m-%d")
    holidays = TW_MARKET_HOLIDAYS.get(year)
    if holidays is None:
        if year not in _holiday_warned_years:
            _holiday_warned_years.add(year)
            logger.warning(f"_is_tw_market_holiday: config.TW_MARKET_HOLIDAYS 沒有 {year} 年的休市日清單，"
                            f"目前只用「是否週末」判斷開盤，平日遇到國定假日會誤判成開盤，需要手動補上該年度清單")
        return False
    return date_str in holidays

def _is_trading_session() -> bool:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.weekday() < 5 and 540 <= now.hour*60+now.minute <= 810 and not _is_tw_market_holiday(now)

def fetch_stock_institutional(code: str, date_str=None) -> Dict:
    return fetch_institutional_flow(date_str).get(code, {})

def get_market_session() -> Dict:
    now = datetime.now(timezone(timedelta(hours=8)))
    m   = now.hour*60+now.minute
    if now.weekday()>=5: return {"session":"weekend","session_zh":"週末休市","is_open":False,"taipei_time":now.strftime("%H:%M")}
    if _is_tw_market_holiday(now): return {"session":"holiday","session_zh":"國定假日休市","is_open":False,"taipei_time":now.strftime("%H:%M")}
    if 540<=m<=810:      return {"session":"trading", "session_zh":"交易時段","is_open":True, "taipei_time":now.strftime("%H:%M")}
    if m<540:            return {"session":"pre_market","session_zh":"盤前",  "is_open":False,"taipei_time":now.strftime("%H:%M")}
    return               {"session":"after_market","session_zh":"盤後",      "is_open":False,"taipei_time":now.strftime("%H:%M")}

def get_fubon_connection_status() -> Dict:
    return {"sdk_available":False,"connected":False,"api_key_set":bool(FUBON_API_KEY),"is_trading":_is_trading_session()}


# ════════════════════════════════════════════════
# 早盤前K線快取增量更新 + 完整性檢查
# ════════════════════════════════════════════════
# ★ 新增：2026-09-24——使用者要求「早盤還沒開始之前先掃描並比對是否和歷史
# K線圖都正確，以後再去做其他動作」。這裡在每天開盤前（見 app.py
# job_premarket_cache_refresh，排在 08:00，早於 09:00 開盤且晚於前一天
# 收盤資料定案的時間）主動把全市場的K線快取補到最新，並對其中一小部分抽樣
# 用「強制重抓、跳過快取」的方式再驗證一次，確保快取沒有壞掉/卡在舊資料——
# 如果早盤前就先把這件事做完，16:30 的正式掃描就能直接吃現成的快取，
# 不用再對 yfinance 重新下載，這是「掃描速度」跟「準確度」同時要顧到的原因：
# 光是增量更新不夠，還要有一個獨立的驗證步驟確認更新真的成功、資料真的對。
def premarket_cache_refresh(sample_validate: int = 20) -> Dict:
    from stock_universe import build_universe
    import concurrent.futures

    universe = build_universe()
    tickers = [s["ticker"] for s in universe]
    total = len(tickers)
    logger.info(f"premarket_cache_refresh: 開始更新 {total} 檔的K線快取")

    success, failed = 0, []
    _CONCURRENCY = 4  # 沿用 scanner.py 的保守並行數，避免早盤前就把 Render 512MB 方案的記憶體/外部API額度用光
    for i in range(0, total, 50):
        batch = tickers[i:i+50]
        with concurrent.futures.ThreadPoolExecutor(max_workers=_CONCURRENCY) as pool:
            futures = {pool.submit(fetch_ohlcv, t, "daily"): t for t in batch}
            for fut in concurrent.futures.as_completed(futures):
                t = futures[fut]
                try:
                    if fut.result():
                        success += 1
                    else:
                        failed.append(t)
                except Exception as e:
                    failed.append(t)
                    logger.warning(f"premarket_cache_refresh: {t} 更新失敗: {e}")
        time.sleep(1)

    # 抽樣完整性驗證：對已更新成功的標的隨機抽一小部分，跳過快取直接對
    # yfinance 重抓最近5天，比對「快取裡的最新收盤價」是否跟「這次重抓到的
    # 最新收盤價」一致，藉此驗證快取沒有卡在舊資料、或被寫壞。
    import random
    from state_store import store
    validated_ok = validated_ticker_count = 0
    mismatches = []
    sample_pool = [t for t in tickers if t not in failed]
    sample = random.sample(sample_pool, min(sample_validate, len(sample_pool))) if sample_pool else []
    for t in sample:
        try:
            cached = store.get_cached_ohlcv_bars(t, "daily", limit=1)
            if not cached:
                continue
            cached_close = cached[-1]["close"]
            if not YFINANCE_OK:
                continue
            h = yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=True)
            if h is None or h.empty:
                continue
            fresh_close = round(float(h["Close"].iloc[-1]), 2)
            validated_ticker_count += 1
            # 千分之一以內的差異視為浮點數/資料源精度落差，不當成不一致
            if abs(fresh_close - cached_close) / max(fresh_close, 0.01) < 0.001:
                validated_ok += 1
            else:
                mismatches.append({"ticker": t, "cached": cached_close, "fresh": fresh_close})
        except Exception as e:
            logger.warning(f"premarket_cache_refresh 驗證 {t}: {e}")

    stats = {
        "total": total, "success": success, "failed_count": len(failed),
        "failed_sample": failed[:20],
        "validated": validated_ticker_count, "validated_ok": validated_ok,
        "mismatches": mismatches,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        f"premarket_cache_refresh: 完成，成功 {success}/{total}（失敗 {len(failed)}），"
        f"抽樣驗證 {validated_ticker_count} 檔，一致 {validated_ok} 檔，不一致 {len(mismatches)} 檔"
    )
    return stats
