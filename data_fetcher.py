"""
data_fetcher.py — 台股數據抓取 v1.0
來源：yfinance + TWSE 官方 API
"""
import time, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False
    logger.error("yfinance 未安裝！pip install yfinance")

from config import TIMEFRAMES, SYSTEM, CIRCUIT_BREAKER as CB

_cache: Dict = {}

def _cache_get(key, ttl):
    e = _cache.get(key)
    return e["data"] if e and time.time()-e["ts"]<ttl else None

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}
    return data

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def fetch_ohlcv(ticker: str, tf_key: str = "daily") -> Optional[Dict]:
    cache_k = f"ohlcv_{ticker}_{tf_key}"
    if c := _cache_get(cache_k, SYSTEM["cache_ttl_sec"]): return c
    if not YFINANCE_OK: return None
    tf = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])
    try:
        h = yf.Ticker(ticker).history(period=tf["period"], interval=tf["interval"], auto_adjust=True)
        if h is None or len(h) < 20: return None
        h = h[h["Volume"] > 0].tail(tf["bars"])
        if len(h) < 20: return None
        closes  = [round(float(x), 2) for x in h["Close"]]
        opens   = [round(float(x), 2) for x in h["Open"]]
        highs   = [round(float(x), 2) for x in h["High"]]
        lows    = [round(float(x), 2) for x in h["Low"]]
        volumes = [int(x // 1000) for x in h["Volume"]]
        dates   = [str(d.date()) for d in h.index]
        if not closes or closes[-1] <= 0: return None
        result = {
            "ticker": ticker, "tf_key": tf_key, "label": tf["label"],
            "closes": closes, "opens": opens, "highs": highs, "lows": lows,
            "volumes": volumes, "dates": dates,
            "current_price": closes[-1],
            "prev_close": closes[-2] if len(closes) > 1 else closes[-1],
            "change_pct": round((closes[-1]-closes[-2])/closes[-2]*100, 2) if len(closes) > 1 else 0,
            "bar_count": len(closes), "source": "yfinance",
        }
        return _cache_set(cache_k, result)
    except Exception as e:
        logger.warning(f"[yf] {ticker} {tf_key}: {e}"); return None

def fetch_all_timeframes(ticker: str) -> Optional[Dict]:
    result = {}
    for tf_key in ["weekly", "daily", "hourly"]:
        d = fetch_ohlcv(ticker, tf_key)
        if d: result[tf_key] = d
        time.sleep(0.3)
    if "daily" not in result:
        logger.warning(f"fetch_all_timeframes {ticker}: 缺少日線"); return None
    return result

def fetch_batch_current_prices(tickers: List[str]) -> Dict[str, float]:
    if not YFINANCE_OK or not tickers: return {}
    prices = {}
    try:
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i+50]
            data = yf.download(" ".join(chunk), period="2d", interval="1d", auto_adjust=True, progress=False)
            if data.empty: continue
            close_data = data["Close"] if "Close" in data else data
            if hasattr(close_data, "columns"):
                for t in chunk:
                    if t in close_data.columns:
                        vals = close_data[t].dropna()
                        if len(vals) > 0: prices[t] = round(float(vals.iloc[-1]), 2)
            time.sleep(0.5)
    except Exception as e:
        logger.error(f"fetch_batch_current_prices: {e}")
    return prices

def fetch_market_index() -> Dict:
    if c := _cache_get("market_index", 300): return c
    result = {}
    if not YFINANCE_OK: return result
    indices = {"twii": "^TWII", "tpex": "^TPEX", "sp500": "^GSPC",
               "nasdaq": "^NDX", "vix": "^VIX", "dxy": "DX-Y.NYB"}
    for key, sym in indices.items():
        try:
            h = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
            if h is None or len(h) < 2: continue
            p = float(h["Close"].iloc[-1]); pv = float(h["Close"].iloc[-2])
            if key == "twii" and not (5000 < p < 30000): continue
            result[key] = {"price": round(p,2), "prev": round(pv,2),
                           "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2), "source": "yfinance"}
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"fetch_market_index {key}: {e}")
    if "twii" in result:
        chg = result["twii"]["chg"]
        result["market_status"] = "stop" if chg <= CB["twii_drop_stop"] else "caution" if chg <= CB["twii_drop_caution"] else "normal"
        result["market_status_zh"] = f"大盤重挫 {chg:.1f}%，暫停多單" if chg <= CB["twii_drop_stop"] else f"大盤下跌 {chg:.1f}%，謹慎" if chg <= CB["twii_drop_caution"] else "大盤正常"
    return _cache_set("market_index", result)

def fetch_institutional_flow(date_str=None) -> Dict:
    cache_k = f"inst_flow_{date_str or 'today'}"
    if c := _cache_get(cache_k, 1800): return c
    if date_str is None: date_str = datetime.now().strftime("%Y%m%d")
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
                def parse_int(v): return int(v.replace(",","").replace("+","")) if v.strip() not in ("-","") else 0
                foreign_net = parse_int(row[4]) if len(row) > 4 else 0
                trust_net   = parse_int(row[7]) if len(row) > 7 else 0
                total_net   = parse_int(row[11]) if len(row) > 11 else 0
                result[code] = {
                    "name": name, "foreign_net": foreign_net,
                    "trust_net": trust_net, "total_net": total_net,
                    "signal": "strong_buy" if foreign_net>500 and trust_net>0 else
                              "buy" if foreign_net>100 else
                              "strong_sell" if foreign_net<-500 else
                              "sell" if foreign_net<-100 else "neutral",
                }
            except (IndexError, ValueError): continue
        logger.info(f"三大法人：{len(result)} 檔")
        return _cache_set(cache_k, result)
    except Exception as e:
        logger.error(f"fetch_institutional_flow: {e}"); return {}

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
        latest = rows[-1]
        net_buy = int(latest[4].replace(",","").replace("+","")) if len(latest) > 4 else 0
        result = {
            "date": latest[0] if rows else "",
            "net_buy_twd": net_buy,
            "net_buy_lots": round(net_buy/1000, 0),
            "signal": "strong_buy" if net_buy>50e8 else "buy" if net_buy>0 else "strong_sell" if net_buy<-50e8 else "sell",
        }
        return _cache_set("foreign_total", result)
    except Exception as e:
        logger.error(f"fetch_foreign_total_flow: {e}"); return {}

def fetch_market_overview() -> Dict:
    if c := _cache_get("market_overview", 1800): return c
    overview = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    overview["index"]         = fetch_market_index()
    overview["foreign"]       = fetch_foreign_total_flow()
    overview["institutional"] = fetch_institutional_flow()
    score = 50
    idx = overview["index"]
    if "twii" in idx:
        chg = idx["twii"]["chg"]
        score += 15 if chg>1.5 else 8 if chg>0.5 else 3 if chg>0 else -15 if chg<-1.5 else -8 if chg<-0.5 else -3
    if "vix" in idx:
        vix = idx["vix"]["price"]
        score += 10 if vix<15 else 5 if vix<20 else -15 if vix>30 else -8 if vix>25 else 0
    fn = overview.get("foreign",{}).get("net_buy_twd",0)
    score += 15 if fn>50e8 else 8 if fn>10e8 else -15 if fn<-50e8 else -8 if fn<-10e8 else 0
    score = max(0, min(100, score))
    overview["sentiment_score"] = score
    overview["sentiment_zh"] = "強烈看多" if score>=80 else "偏多" if score>=60 else "中性" if score>=40 else "偏空" if score>=20 else "強烈看空"
    overview["can_trade"]     = overview["index"].get("market_status","normal") != "stop"
    overview["market_status"] = overview["index"].get("market_status","normal")
    return _cache_set("market_overview", overview)

def fetch_stock_institutional(code: str, date_str=None) -> Dict:
    return fetch_institutional_flow(date_str).get(code, {})

def get_market_session() -> Dict:
    now_tw = datetime.now(timezone(timedelta(hours=8)))
    wd = now_tw.weekday()
    if wd >= 5: return {"session":"weekend","session_zh":"週末休市","is_open":False}
    total_min = now_tw.hour*60 + now_tw.minute
    if 540 <= total_min <= 810:
        return {"session":"trading","session_zh":"交易時段","is_open":True,"taipei_time":now_tw.strftime("%H:%M")}
    elif total_min < 540:
        return {"session":"pre_market","session_zh":"盤前","is_open":False}
    else:
        return {"session":"after_market","session_zh":"盤後","is_open":False}
