"""
data_fetcher.py v2.2
修正：
★ TWII 改用 TWSE 官方 API（不依賴 yfinance ticker）
★ TPEX 加 SSL verify=False
★ 黃金改用 GC=F + CoinGecko 備援
★ yfinance download 格式修正
★ 快取 5 分鐘更新
"""
import time, logging, requests, warnings
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
from config import TIMEFRAMES, SYSTEM, CIRCUIT_BREAKER as CB

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

def _cache_get(key, ttl):
    e = _cache.get(key)
    return e["data"] if e and time.time()-e["ts"]<ttl else None

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}
    return data

def _cache_clear(key):
    _cache.pop(key, None)

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
    tpex = _fetch_tpex()
    if tpex:
        result["tpex"] = tpex

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
def fetch_ohlcv(ticker: str, tf_key: str = "daily") -> Optional[Dict]:
    cache_k = f"ohlcv_{ticker}_{tf_key}"
    if c := _cache_get(cache_k, SYSTEM["cache_ttl_sec"]):
        return c
    if not YFINANCE_OK:
        return None
    tf = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])
    try:
        h = yf.Ticker(ticker).history(period=tf["period"], interval=tf["interval"], auto_adjust=True)
        if h is None or h.empty or len(h) < 20:
            return None
        h = h[h["Volume"] > 0].tail(tf["bars"])
        if len(h) < 20:
            return None
        closes  = [round(float(x),2) for x in h["Close"]]
        opens   = [round(float(x),2) for x in h["Open"]]
        highs   = [round(float(x),2) for x in h["High"]]
        lows    = [round(float(x),2) for x in h["Low"]]
        volumes = [int(x//1000) for x in h["Volume"]]
        dates   = [str(d.date()) for d in h.index]
        if not closes or closes[-1] <= 0:
            return None
        return _cache_set(cache_k, {
            "ticker": ticker, "tf_key": tf_key, "label": tf["label"],
            "closes": closes, "opens": opens, "highs": highs, "lows": lows,
            "volumes": volumes, "dates": dates,
            "current_price": closes[-1],
            "prev_close":    closes[-2] if len(closes)>1 else closes[-1],
            "change_pct":    round((closes[-1]-closes[-2])/closes[-2]*100,2) if len(closes)>1 else 0,
            "bar_count":     len(closes), "source": "yfinance",
        })
    except Exception as e:
        logger.warning(f"fetch_ohlcv {ticker} {tf_key}: {e}")
        return None

def fetch_all_timeframes(ticker: str) -> Optional[Dict]:
    result = {}
    for tf_key in ["weekly", "daily", "hourly"]:
        d = fetch_ohlcv(ticker, tf_key)
        if d: result[tf_key] = d
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
# 市場總覽
# ════════════════════════════════════════════════
def fetch_market_overview() -> Dict:
    if c := _cache_get("market_overview", 300): return c
    overview = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    overview["index"]         = fetch_market_index()
    overview["foreign"]       = fetch_foreign_total_flow()
    overview["institutional"] = fetch_institutional_flow()
    score = 50
    twii_chg = overview["index"].get("twii",{}).get("chg",0)
    score += 15 if twii_chg>1.5 else 8 if twii_chg>0.5 else 3 if twii_chg>0 else -15 if twii_chg<-1.5 else -8 if twii_chg<-0.5 else -3
    vix_p = overview["index"].get("vix",{}).get("price",20)
    score += 10 if vix_p<15 else 5 if vix_p<20 else -15 if vix_p>30 else -8 if vix_p>25 else 0
    fn = overview.get("foreign",{}).get("net_buy_twd",0)
    score += 15 if fn>50e8 else 8 if fn>10e8 else -15 if fn<-50e8 else -8 if fn<-10e8 else 0
    score = max(0, min(100, score))
    overview["sentiment_score"] = score
    overview["sentiment_zh"]    = "強烈看多" if score>=80 else "偏多" if score>=60 else "中性" if score>=40 else "偏空" if score>=20 else "強烈看空"
    overview["can_trade"]       = overview["index"].get("market_status","normal") != "stop"
    overview["market_status"]   = overview["index"].get("market_status","normal")
    overview["is_trading"]      = _is_trading_session()
    return _cache_set("market_overview", overview)

def _is_trading_session() -> bool:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.weekday() < 5 and 540 <= now.hour*60+now.minute <= 810

def fetch_stock_institutional(code: str, date_str=None) -> Dict:
    return fetch_institutional_flow(date_str).get(code, {})

def get_market_session() -> Dict:
    now = datetime.now(timezone(timedelta(hours=8)))
    m   = now.hour*60+now.minute
    if now.weekday()>=5: return {"session":"weekend","session_zh":"週末休市","is_open":False,"taipei_time":now.strftime("%H:%M")}
    if 540<=m<=810:      return {"session":"trading", "session_zh":"交易時段","is_open":True, "taipei_time":now.strftime("%H:%M")}
    if m<540:            return {"session":"pre_market","session_zh":"盤前",  "is_open":False,"taipei_time":now.strftime("%H:%M")}
    return               {"session":"after_market","session_zh":"盤後",      "is_open":False,"taipei_time":now.strftime("%H:%M")}

def get_fubon_connection_status() -> Dict:
    return {"sdk_available":False,"connected":False,"api_key_set":bool(FUBON_API_KEY),"is_trading":_is_trading_session()}
