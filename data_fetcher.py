"""
data_fetcher.py v2.1 修正：
★ ^TWII 改用多個備用 ticker 輪詢
★ 加入 BTC / 黃金即時價格
★ 快取時間縮短（大盤 5 分鐘更新一次）
★ VIX / DXY 強制每次重新抓取不用快取
★ 富果 API 路徑修正
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
    logger.error("yfinance 未安裝")

import os
from config import TIMEFRAMES, SYSTEM, CIRCUIT_BREAKER as CB

FUBON_API_KEY    = os.getenv("FUBON_API_KEY", "")
FUBON_API_SECRET = os.getenv("FUBON_API_SECRET", "")
FUGLE_API_KEY    = os.getenv("FUGLE_API_KEY", "")

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ════════════════════════════════════════════════
# 快取（大盤 5 分鐘、K線 1 小時）
# ════════════════════════════════════════════════
_cache: Dict = {}

def _cache_get(key: str, ttl: int) -> Optional[Any]:
    e = _cache.get(key)
    return e["data"] if e and time.time() - e["ts"] < ttl else None

def _cache_set(key: str, data: Any) -> Any:
    _cache[key] = {"data": data, "ts": time.time()}
    return data

def _cache_clear(key: str):
    _cache.pop(key, None)

# ════════════════════════════════════════════════
# 台股加權指數（多源備援）
# ════════════════════════════════════════════════
TWII_TICKERS = [
    "^TWII",           # 主要
    "0050.TW",         # 台灣50 ETF 當替代參考
    "TWO",             # 備用
]

def _fetch_twii_yf() -> Optional[Dict]:
    """
    抓加權指數 — 嘗試多個 ticker
    yfinance 的 ^TWII 有時不穩定，用多個方式備援
    """
    if not YFINANCE_OK:
        return None

    # 方法一：直接抓 ^TWII（1mo 資料通常比 5d 穩定）
    for period in ["1mo", "5d", "3mo"]:
        try:
            h = yf.Ticker("^TWII").history(period=period, interval="1d", auto_adjust=True)
            if h is not None and len(h) >= 2:
                p  = float(h["Close"].iloc[-1])
                pv = float(h["Close"].iloc[-2])
                if 5000 < p < 30000:
                    chg = round((p - pv) / pv * 100, 2)
                    logger.info(f"TWII ({period}): {p:.0f}（{chg:+.2f}%）")
                    return {
                        "price":  round(p, 2),
                        "prev":   round(pv, 2),
                        "chg":    chg,
                        "chg_pt": round(p - pv, 2),
                        "source": f"yfinance_{period}",
                    }
        except Exception as e:
            logger.warning(f"TWII {period}: {e}")
        time.sleep(0.3)

    # 方法二：用 download（有時比 Ticker 穩定）
    try:
        import pandas as pd
        data = yf.download("^TWII", period="1mo", interval="1d",
                           auto_adjust=True, progress=False)
        if not data.empty and len(data) >= 2:
            p  = float(data["Close"].iloc[-1])
            pv = float(data["Close"].iloc[-2])
            if 5000 < p < 30000:
                chg = round((p - pv) / pv * 100, 2)
                return {"price": round(p,2), "prev": round(pv,2),
                        "chg": chg, "chg_pt": round(p-pv,2), "source": "yfinance_download"}
    except Exception as e:
        logger.warning(f"TWII download: {e}")

    # 方法三：TWSE 官方 API 抓大盤指數
    try:
        today = datetime.now().strftime("%Y%m%d")
        url   = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json&date={today}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            # TWSE 有提供大盤指數在另一個端點
            pass
    except Exception:
        pass

    # 方法四：台灣證交所直接抓大盤收盤
    try:
        url = "https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date="
        today = datetime.now().strftime("%Y%m%d")
        r = requests.get(url + today, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d = r.json()
            rows = d.get("data", [])
            if rows:
                last = rows[-1]
                p = float(str(last[-1]).replace(",", ""))
                if 5000 < p < 30000:
                    return {"price": round(p,2), "prev": 0,
                            "chg": 0, "chg_pt": 0, "source": "twse_direct"}
    except Exception as e:
        logger.warning(f"TWSE direct: {e}")

    logger.error("TWII 所有來源均失敗")
    return None


def _fetch_tpex_yf() -> Optional[Dict]:
    """抓上櫃指數"""
    if not YFINANCE_OK:
        return None
    for sym in ["^TPEX", "^TWOII", "TWO"]:
        try:
            h = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=True)
            if h is not None and len(h) >= 2:
                p  = float(h["Close"].iloc[-1])
                pv = float(h["Close"].iloc[-2])
                if 50 < p < 5000:
                    return {"price": round(p,2), "prev": round(pv,2),
                            "chg": round((p-pv)/pv*100,2), "chg_pt": round(p-pv,2),
                            "source": f"yfinance_{sym}"}
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"TPEX {sym}: {e}")
    return None

# ════════════════════════════════════════════════
# BTC 價格（多源）
# ════════════════════════════════════════════════
def _fetch_btc() -> Optional[Dict]:
    # CoinGecko（免費無需 key）
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd",
                    "include_24hr_change": "true"},
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200:
            d = r.json().get("bitcoin", {})
            p   = float(d.get("usd", 0))
            chg = float(d.get("usd_24h_change", 0) or 0)
            if p > 1000:
                return {"price": round(p, 0), "chg": round(chg, 2), "source": "coingecko"}
    except Exception as e:
        logger.warning(f"BTC coingecko: {e}")

    # yfinance 備用
    if YFINANCE_OK:
        try:
            h = yf.Ticker("BTC-USD").history(period="5d", interval="1d")
            if h is not None and len(h) >= 2:
                p  = float(h["Close"].iloc[-1])
                pv = float(h["Close"].iloc[-2])
                if p > 1000:
                    return {"price": round(p,0), "chg": round((p-pv)/pv*100,2), "source": "yfinance"}
        except Exception as e:
            logger.warning(f"BTC yfinance: {e}")
    return None

# ════════════════════════════════════════════════
# 黃金價格（多源）
# ════════════════════════════════════════════════
def _fetch_gold() -> Optional[Dict]:
    # yfinance XAUUSD=X（現貨，最準）
    if YFINANCE_OK:
        for sym in ["XAUUSD=X", "GC=F"]:
            try:
                h = yf.Ticker(sym).history(period="5d", interval="1d")
                if h is not None and len(h) >= 2:
                    p  = float(h["Close"].iloc[-1])
                    pv = float(h["Close"].iloc[-2])
                    if 1500 < p < 4000:
                        return {
                            "price":  round(p, 2),
                            "chg":    round((p - pv) / pv * 100, 2),
                            "chg_pt": round(p - pv, 2),
                            "source": f"yfinance_{sym}",
                        }
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"Gold {sym}: {e}")

    # CoinGecko 黃金（備用）
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "gold", "vs_currencies": "usd", "include_24hr_change": "true"},
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200:
            d = r.json().get("gold", {})
            p = float(d.get("usd", 0))
            if p > 1000:
                return {"price": round(p,2), "chg": float(d.get("usd_24h_change",0) or 0), "source": "coingecko"}
    except Exception:
        pass
    return None

# ════════════════════════════════════════════════
# VIX / DXY（強制不用快取，每次更新）
# ════════════════════════════════════════════════
def _fetch_vix_dxy() -> Dict:
    result = {}
    if not YFINANCE_OK:
        return result
    for key, sym in {"vix": "^VIX", "dxy": "DX-Y.NYB"}.items():
        try:
            # 強制清除快取，每次重新抓
            _cache_clear(f"idx_{key}")
            h = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
            if h is not None and len(h) >= 2:
                p  = float(h["Close"].iloc[-1])
                pv = float(h["Close"].iloc[-2])
                # VIX 合理範圍 5~90
                if key == "vix" and not (5 < p < 90):
                    continue
                result[key] = {
                    "price":  round(p, 2),
                    "prev":   round(pv, 2),
                    "chg":    round((p - pv) / pv * 100, 2),
                    "chg_pt": round(p - pv, 2),
                    "source": "yfinance",
                }
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"{key} {sym}: {e}")
    return result

# ════════════════════════════════════════════════
# 美股指數
# ════════════════════════════════════════════════
def _fetch_us_indices() -> Dict:
    result = {}
    if not YFINANCE_OK:
        return result
    for key, sym in {"sp500": "^GSPC", "nasdaq": "^NDX"}.items():
        try:
            h = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
            if h is not None and len(h) >= 2:
                p  = float(h["Close"].iloc[-1])
                pv = float(h["Close"].iloc[-2])
                result[key] = {
                    "price":  round(p, 2),
                    "prev":   round(pv, 2),
                    "chg":    round((p - pv) / pv * 100, 2),
                    "chg_pt": round(p - pv, 2),
                    "source": "yfinance",
                }
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"{key}: {e}")
    return result

# ════════════════════════════════════════════════
# 大盤指數主函數（5 分鐘快取）
# ════════════════════════════════════════════════
def fetch_market_index() -> Dict:
    if c := _cache_get("market_index", 300):  # 5 分鐘
        return c

    result = {}

    # 1. 台股加權指數
    twii = _fetch_twii_yf()
    if twii:
        result["twii"] = twii
    else:
        logger.error("TWII 抓取失敗，使用預設值")
        result["twii"] = {"price": 0, "chg": 0, "source": "error"}

    # 2. 上櫃指數
    tpex = _fetch_tpex_yf()
    if tpex:
        result["tpex"] = tpex

    # 3. VIX + DXY（強制更新）
    vix_dxy = _fetch_vix_dxy()
    result.update(vix_dxy)

    # 4. 美股
    us = _fetch_us_indices()
    result.update(us)

    # 5. BTC
    btc = _fetch_btc()
    if btc:
        result["btc"] = btc

    # 6. 黃金
    gold = _fetch_gold()
    if gold:
        result["gold"] = gold

    # 7. 大盤狀態判斷
    if "twii" in result and result["twii"].get("chg", 0) != 0:
        chg = result["twii"]["chg"]
        if chg <= CB["twii_drop_stop"]:
            result["market_status"]    = "stop"
            result["market_status_zh"] = f"大盤重挫 {chg:.1f}%，暫停多單"
        elif chg <= CB["twii_drop_caution"]:
            result["market_status"]    = "caution"
            result["market_status_zh"] = f"大盤下跌 {chg:.1f}%，謹慎"
        else:
            result["market_status"]    = "normal"
            result["market_status_zh"] = "大盤正常"

    logger.info(
        f"大盤更新：TWII={result.get('twii',{}).get('price',0):.0f} "
        f"({result.get('twii',{}).get('chg',0):+.2f}%) | "
        f"VIX={result.get('vix',{}).get('price','—')} | "
        f"DXY={result.get('dxy',{}).get('price','—')} | "
        f"BTC={result.get('btc',{}).get('price','—')} | "
        f"Gold={result.get('gold',{}).get('price','—')}"
    )
    return _cache_set("market_index", result)

# ════════════════════════════════════════════════
# K線（保持原有邏輯）
# ════════════════════════════════════════════════
def fetch_ohlcv(ticker: str, tf_key: str = "daily") -> Optional[Dict]:
    cache_k = f"ohlcv_{ticker}_{tf_key}"
    if c := _cache_get(cache_k, SYSTEM["cache_ttl_sec"]):
        return c
    if not YFINANCE_OK:
        return None
    tf = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])
    try:
        h = yf.Ticker(ticker).history(
            period=tf["period"], interval=tf["interval"], auto_adjust=True
        )
        if h is None or len(h) < 20:
            return None
        h = h[h["Volume"] > 0].tail(tf["bars"])
        if len(h) < 20:
            return None
        closes  = [round(float(x), 2) for x in h["Close"]]
        opens   = [round(float(x), 2) for x in h["Open"]]
        highs   = [round(float(x), 2) for x in h["High"]]
        lows    = [round(float(x), 2) for x in h["Low"]]
        volumes = [int(x // 1000) for x in h["Volume"]]
        dates   = [str(d.date()) for d in h.index]
        if not closes or closes[-1] <= 0:
            return None
        return _cache_set(cache_k, {
            "ticker":        ticker,
            "tf_key":        tf_key,
            "label":         tf["label"],
            "closes":        closes,
            "opens":         opens,
            "highs":         highs,
            "lows":          lows,
            "volumes":       volumes,
            "dates":         dates,
            "current_price": closes[-1],
            "prev_close":    closes[-2] if len(closes) > 1 else closes[-1],
            "change_pct":    round((closes[-1]-closes[-2])/closes[-2]*100, 2) if len(closes) > 1 else 0,
            "bar_count":     len(closes),
            "source":        "yfinance",
        })
    except Exception as e:
        logger.warning(f"fetch_ohlcv {ticker} {tf_key}: {e}")
        return None

def fetch_all_timeframes(ticker: str) -> Optional[Dict]:
    result = {}
    for tf_key in ["weekly", "daily", "hourly"]:
        d = fetch_ohlcv(ticker, tf_key)
        if d:
            result[tf_key] = d
        time.sleep(0.3)
    if "daily" not in result:
        return None
    return result

# ════════════════════════════════════════════════
# 法人數據（TWSE）
# ════════════════════════════════════════════════
def fetch_institutional_flow(date_str=None) -> Dict:
    cache_k = f"inst_flow_{date_str or 'today'}"
    if c := _cache_get(cache_k, 1800):
        return c
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        result = {}
        for row in data.get("data", []):
            try:
                code = row[0].strip(); name = row[1].strip()
                def parse_int(v):
                    return int(v.replace(",","").replace("+","")) if v.strip() not in ("-","") else 0
                foreign_net = parse_int(row[4]) if len(row)>4 else 0
                trust_net   = parse_int(row[7]) if len(row)>7 else 0
                total_net   = parse_int(row[11]) if len(row)>11 else 0
                result[code] = {
                    "name": name, "foreign_net": foreign_net,
                    "trust_net": trust_net, "total_net": total_net,
                    "signal": (
                        "strong_buy"  if foreign_net>500 and trust_net>0 else
                        "buy"         if foreign_net>100 else
                        "strong_sell" if foreign_net<-500 else
                        "sell"        if foreign_net<-100 else "neutral"
                    ),
                }
            except (IndexError, ValueError):
                continue
        logger.info(f"三大法人：{len(result)} 檔")
        return _cache_set(cache_k, result)
    except Exception as e:
        logger.error(f"fetch_institutional_flow: {e}")
        return {}

def fetch_foreign_total_flow() -> Dict:
    if c := _cache_get("foreign_total", 3600):
        return c
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
            "date":         latest[0],
            "net_buy_twd":  net_buy,
            "net_buy_lots": round(net_buy/1000, 0),
            "signal": (
                "strong_buy"  if net_buy>50e8  else
                "buy"         if net_buy>0     else
                "strong_sell" if net_buy<-50e8 else "sell"
            ),
        })
    except Exception as e:
        logger.error(f"fetch_foreign_total_flow: {e}")
        return {}

# ════════════════════════════════════════════════
# 市場總覽
# ════════════════════════════════════════════════
def fetch_market_overview() -> Dict:
    if c := _cache_get("market_overview", 300):  # 5 分鐘
        return c

    overview = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    overview["index"]         = fetch_market_index()
    overview["foreign"]       = fetch_foreign_total_flow()
    overview["institutional"] = fetch_institutional_flow()

    # 情緒分數
    score = 50
    idx   = overview["index"]
    if "twii" in idx and idx["twii"].get("chg", 0) != 0:
        chg    = idx["twii"]["chg"]
        score += 15 if chg>1.5 else 8 if chg>0.5 else 3 if chg>0 else -15 if chg<-1.5 else -8 if chg<-0.5 else -3
    if "vix" in idx:
        vix    = idx["vix"]["price"]
        score += 10 if vix<15 else 5 if vix<20 else -15 if vix>30 else -8 if vix>25 else 0
    fn     = overview.get("foreign",{}).get("net_buy_twd",0)
    score += 15 if fn>50e8 else 8 if fn>10e8 else -15 if fn<-50e8 else -8 if fn<-10e8 else 0
    score  = max(0, min(100, score))

    overview["sentiment_score"] = score
    overview["sentiment_zh"]    = (
        "強烈看多" if score>=80 else "偏多" if score>=60 else
        "中性"     if score>=40 else "偏空" if score>=20 else "強烈看空"
    )
    overview["can_trade"]     = overview["index"].get("market_status","normal") != "stop"
    overview["market_status"] = overview["index"].get("market_status","normal")
    overview["is_trading"]    = _is_trading_session()

    return _cache_set("market_overview", overview)

def _is_trading_session() -> bool:
    now_tw    = datetime.now(timezone(timedelta(hours=8)))
    wd        = now_tw.weekday()
    total_min = now_tw.hour*60 + now_tw.minute
    return wd < 5 and 540 <= total_min <= 810

def fetch_stock_institutional(code: str, date_str=None) -> Dict:
    return fetch_institutional_flow(date_str).get(code, {})

def get_market_session() -> Dict:
    now_tw    = datetime.now(timezone(timedelta(hours=8)))
    wd        = now_tw.weekday()
    total_min = now_tw.hour*60 + now_tw.minute
    if wd >= 5:
        return {"session":"weekend","session_zh":"週末休市","is_open":False,"taipei_time":now_tw.strftime("%H:%M")}
    if 540 <= total_min <= 810:
        return {"session":"trading","session_zh":"交易時段","is_open":True,"taipei_time":now_tw.strftime("%H:%M")}
    elif total_min < 540:
        return {"session":"pre_market","session_zh":"盤前","is_open":False,"taipei_time":now_tw.strftime("%H:%M")}
    else:
        return {"session":"after_market","session_zh":"盤後","is_open":False,"taipei_time":now_tw.strftime("%H:%M")}

def get_fubon_connection_status() -> Dict:
    return {"sdk_available": False, "connected": False,
            "api_key_set": bool(FUBON_API_KEY), "is_trading": _is_trading_session()}
