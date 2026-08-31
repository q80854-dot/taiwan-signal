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
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

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
                if 5000 < p < 30000:
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
                if 5000 < p < 30000:
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
                    if 5000 < p < 30000:
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
    try:
        today = datetime.now().strftime("%Y%m%d")
        url   = f"https://www.tpex.org.tw/web/stock/aftertrading/market_summary/summary_result.php?l=zh-tw&d={today[:4]}/{today[4:6]}/{today[6:]}&o=json"
        r = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        if r.status_code == 200:
            d = r.json()
            items = d.get("aaData", [])
            if items:
                # 上櫃指數通常在第一行
                row = items[0]
                p = float(str(row[1]).replace(",","")) if len(row) > 1 else 0
                if 50 < p < 5000:
                    return {"price": round(p,2), "prev": round(p,2),
                            "chg": 0, "chg_pt": 0, "source": "tpex_official"}
    except Exception as e:
        logger.warning(f"TPEX official: {e}")

    # yfinance 備用
    if YFINANCE_OK:
        for sym in ["^TPEX", "^TWOII"]:
            try:
                h = yf.Ticker(sym).history(period="3mo", interval="1d")
                if h is not None and not h.empty and len(h) >= 2:
                    p  = float(h["Close"].iloc[-1])
                    pv = float(h["Close"].iloc[-2])
                    if 50 < p < 5000:
                        return {"price": round(p,2), "prev": round(pv,2),
                                "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2),
                                "source": f"yfinance_{sym}"}
                time.sleep(0.2)
            except Exception:
                pass
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
        for row in data.get("data", []):
            try:
                code = row[0].strip(); name = row[1].strip()
                def pi(v): return int(v.replace(",","").replace("+","")) if v.strip() not in ("-","") else 0
                fn = pi(row[4]) if len(row)>4 else 0
                tn = pi(row[7]) if len(row)>7 else 0
                tt = pi(row[11]) if len(row)>11 else 0
                result[code] = {"name":name,"foreign_net":fn,"trust_net":tn,"total_net":tt,
                                "signal":"strong_buy" if fn>500 and tn>0 else "buy" if fn>100 else "strong_sell" if fn<-500 else "sell" if fn<-100 else "neutral"}
            except: continue
        logger.info(f"三大法人：{len(result)} 檔")
        return _cache_set(cache_k, result)
    except Exception as e:
        logger.error(f"inst_flow: {e}"); return {}

def fetch_foreign_total_flow() -> Dict:
    if c := _cache_get("foreign_total", 3600): return c
    url = "https://www.twse.com.tw/fund/MI_QFIIS?response=json&selectType=Daily"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200: return {}
        data = r.json()
        if data.get("stat") != "OK": return {}
        rows = data.get("data", [])
        if not rows: return {}
        latest  = rows[-1]
        net_buy = int(latest[4].replace(",","").replace("+","")) if len(latest)>4 else 0
        return _cache_set("foreign_total", {
            "date": latest[0], "net_buy_twd": net_buy,
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
