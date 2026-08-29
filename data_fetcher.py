"""
data_fetcher.py v2.0 — 富邦 Neo 為主，yfinance 為輔
數據互補邏輯：
  盤中（09:00-13:30）→ 富邦 Neo 即時 → yfinance 備用
  盤後/盤前 → yfinance 收盤數據 → TWSE API 法人數據
交叉驗證：兩個來源差異 > 1% 自動警告
"""
import time, math, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False
    logger.error("yfinance 未安裝")

# ── 嘗試載入富邦 Neo SDK ──
try:
    from fubon_neo.sdk import FubonSDK
    FUBON_SDK_AVAILABLE = True
except ImportError:
    FUBON_SDK_AVAILABLE = False

import os
from config import TIMEFRAMES, SYSTEM, CIRCUIT_BREAKER as CB

FUBON_API_KEY    = os.getenv("FUBON_API_KEY", "")
FUBON_API_SECRET = os.getenv("FUBON_API_SECRET", "")
FUBON_ACCOUNT    = os.getenv("FUBON_ACCOUNT", "")
FUBON_CERT_PATH  = os.getenv("FUBON_CERT_PATH", "")
FUBON_CERT_PASS  = os.getenv("FUBON_CERT_PASS", "")

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ════════════════════════════════════════════════
# 快取
# ════════════════════════════════════════════════
_cache: Dict = {}

def _cache_get(key: str, ttl: int) -> Optional[Any]:
    e = _cache.get(key)
    return e["data"] if e and time.time() - e["ts"] < ttl else None

def _cache_set(key: str, data: Any) -> Any:
    _cache[key] = {"data": data, "ts": time.time()}
    return data

# ════════════════════════════════════════════════
# 時段判斷
# ════════════════════════════════════════════════
def _is_trading_session() -> bool:
    """判斷目前是否為台股盤中時段"""
    now_tw = datetime.now(timezone(timedelta(hours=8)))
    wd = now_tw.weekday()
    if wd >= 5:
        return False
    total_min = now_tw.hour * 60 + now_tw.minute
    return 540 <= total_min <= 810  # 09:00~13:30

# ════════════════════════════════════════════════
# 富邦 Neo SDK 連線管理
# ════════════════════════════════════════════════
_fubon_sdk = None
_fubon_logged = False

def _connect_fubon() -> bool:
    global _fubon_sdk, _fubon_logged
    if _fubon_logged:
        return True
    if not FUBON_SDK_AVAILABLE or not all([FUBON_API_KEY, FUBON_API_SECRET]):
        return False
    try:
        _fubon_sdk = FubonSDK()
        kwargs = {
            "id":         FUBON_API_KEY,
            "trade_pass": FUBON_API_SECRET,
        }
        if FUBON_CERT_PATH and os.path.exists(FUBON_CERT_PATH):
            kwargs["cert_path"] = FUBON_CERT_PATH
            kwargs["cert_pass"]  = FUBON_CERT_PASS
        result = _fubon_sdk.login(**kwargs)
        if result.is_success:
            _fubon_logged = True
            logger.info("✅ 富邦 Neo SDK 連線成功")
            return True
        logger.warning(f"富邦登入失敗：{result.message}")
        return False
    except Exception as e:
        logger.warning(f"富邦 Neo 連線異常：{e}")
        return False

def _fubon_get_quote(code: str) -> Optional[Dict]:
    """富邦 Neo 即時報價（盤中用）"""
    if not _fubon_logged and not _connect_fubon():
        return None
    try:
        result = _fubon_sdk.marketdata.intraday.ticker(symbol=code)
        if result.is_success:
            d = result.data
            price = float(d.get("close") or d.get("lastPrice") or 0)
            if price <= 0:
                return None
            return {
                "code":        code,
                "price":       round(price, 2),
                "open":        float(d.get("open", 0)),
                "high":        float(d.get("high", 0)),
                "low":         float(d.get("low", 0)),
                "chg":         float(d.get("change", 0)),
                "chg_pct":     float(d.get("changePercent", 0)),
                "volume_lots": int(d.get("volume", 0)) // 1000,
                "source":      "fubon_neo",
            }
    except Exception as e:
        logger.warning(f"富邦 get_quote {code}: {e}")
    return None

def _fubon_get_candles(code: str, timeframe: str = "D") -> Optional[Dict]:
    """富邦 Neo 歷史 K 線"""
    if not _fubon_logged and not _connect_fubon():
        return None
    try:
        result = _fubon_sdk.marketdata.historical.candles(
            symbol=code, timeframe=timeframe
        )
        if result.is_success:
            bars = result.data or []
            if len(bars) < 20:
                return None
            return {
                "closes":  [float(b.get("close", 0)) for b in bars],
                "opens":   [float(b.get("open", 0))  for b in bars],
                "highs":   [float(b.get("high", 0))  for b in bars],
                "lows":    [float(b.get("low", 0))   for b in bars],
                "volumes": [int(b.get("volume", 0)) // 1000 for b in bars],
                "bar_count": len(bars),
                "current_price": float(bars[-1].get("close", 0)),
                "source":  "fubon_neo",
            }
    except Exception as e:
        logger.warning(f"富邦 get_candles {code}: {e}")
    return None

# ════════════════════════════════════════════════
# 交叉驗證
# ════════════════════════════════════════════════
def _cross_validate(fubon_price: float, yf_price: float, ticker: str) -> float:
    """
    兩個來源交叉驗證
    差異 > 1% 發警告，以富邦為主
    """
    if fubon_price <= 0:
        return yf_price
    if yf_price <= 0:
        return fubon_price
    diff_pct = abs(fubon_price - yf_price) / yf_price * 100
    if diff_pct > 1.0:
        logger.warning(
            f"[CrossValidate] {ticker} 數據差異 {diff_pct:.2f}%："
            f"富邦={fubon_price} / yfinance={yf_price}，以富邦為準"
        )
    return fubon_price  # 富邦為主

# ════════════════════════════════════════════════
# 主要 K 線抓取（互補邏輯）
# ════════════════════════════════════════════════
def fetch_ohlcv(ticker: str, tf_key: str = "daily") -> Optional[Dict]:
    """
    OHLCV 數據抓取（富邦為主，yfinance 為備）
    ticker: 如 "2330.TW"
    """
    cache_k = f"ohlcv_{ticker}_{tf_key}"
    if c := _cache_get(cache_k, SYSTEM["cache_ttl_sec"]):
        return c

    code = ticker.replace(".TW", "").replace(".TWO", "")
    tf   = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])

    # ── 1. 盤中：嘗試富邦即時 ──
    if _is_trading_session() and tf_key in ("daily", "hourly"):
        fubon_q = _fubon_get_quote(code)
        if fubon_q:
            # 盤中即時：只有最新一根，其餘用 yfinance 補歷史
            yf_data = _fetch_yf_ohlcv(ticker, tf_key)
            if yf_data:
                # 用富邦當前價格替換最後一根收盤
                yf_data["closes"][-1]       = fubon_q["price"]
                yf_data["current_price"]     = fubon_q["price"]
                yf_data["source"]            = "fubon_neo+yfinance"
                # 交叉驗證
                yf_last = yf_data.get("prev_close", fubon_q["price"])
                _cross_validate(fubon_q["price"], yf_last, ticker)
                return _cache_set(cache_k, yf_data)

    # ── 2. 富邦歷史 K 線（有帳戶時）──
    if FUBON_SDK_AVAILABLE and FUBON_API_KEY:
        tf_map = {"daily": "D", "weekly": "W", "hourly": "60"}
        fubon_tf = tf_map.get(tf_key, "D")
        fubon_bars = _fubon_get_candles(code, fubon_tf)
        if fubon_bars and fubon_bars["bar_count"] >= 30:
            fubon_bars["ticker"]        = ticker
            fubon_bars["tf_key"]        = tf_key
            fubon_bars["label"]         = tf.get("label", tf_key)
            fubon_bars["change_pct"]    = round(
                (fubon_bars["closes"][-1] - fubon_bars["closes"][-2]) /
                fubon_bars["closes"][-2] * 100, 2
            ) if len(fubon_bars["closes"]) >= 2 else 0
            fubon_bars["prev_close"]    = fubon_bars["closes"][-2] if len(fubon_bars["closes"]) >= 2 else 0
            return _cache_set(cache_k, fubon_bars)

    # ── 3. yfinance 備用 ──
    yf_data = _fetch_yf_ohlcv(ticker, tf_key)
    if yf_data:
        return _cache_set(cache_k, yf_data)

    logger.warning(f"fetch_ohlcv {ticker}/{tf_key}: 所有來源失敗")
    return None


def _fetch_yf_ohlcv(ticker: str, tf_key: str) -> Optional[Dict]:
    """yfinance K 線抓取"""
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
        return {
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
            "change_pct":    round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) > 1 else 0,
            "bar_count":     len(closes),
            "source":        "yfinance",
        }
    except Exception as e:
        logger.warning(f"_fetch_yf_ohlcv {ticker} {tf_key}: {e}")
        return None


def fetch_all_timeframes(ticker: str) -> Optional[Dict]:
    result = {}
    for tf_key in ["weekly", "daily", "hourly"]:
        d = fetch_ohlcv(ticker, tf_key)
        if d:
            result[tf_key] = d
        time.sleep(0.3)
    if "daily" not in result:
        logger.warning(f"fetch_all_timeframes {ticker}: 缺少日線")
        return None
    return result

# ════════════════════════════════════════════════
# 大盤指數（富邦盤中 + yfinance 盤後互補）
# ════════════════════════════════════════════════
def fetch_market_index() -> Dict:
    if c := _cache_get("market_index", 300):
        return c
    result = {}
    is_trading = _is_trading_session()

    # ── 富邦盤中大盤指數 ──
    if is_trading and _connect_fubon():
        for key, sym in {"twii": "TAIEX", "tpex": "TPEx"}.items():
            try:
                r = _fubon_sdk.marketdata.intraday.ticker(symbol=sym)
                if r.is_success:
                    d = r.data
                    p  = float(d.get("close") or d.get("lastPrice") or 0)
                    pv = float(d.get("previousClose") or 0)
                    if 5000 < p < 30000:
                        result[key] = {
                            "price":  round(p, 2),
                            "prev":   round(pv, 2),
                            "chg":    round((p - pv) / pv * 100, 2) if pv else 0,
                            "chg_pt": round(p - pv, 2),
                            "source": "fubon_neo",
                        }
            except Exception as e:
                logger.warning(f"富邦大盤 {key}: {e}")

    # ── yfinance 補充（美股/VIX/DXY 和缺漏的台股指數）──
    yf_indices = {}
    if YFINANCE_OK:
        needed = {
            "twii":   "^TWII",
            "tpex":   "^TPEX",
            "sp500":  "^GSPC",
            "nasdaq": "^NDX",
            "vix":    "^VIX",
            "dxy":    "DX-Y.NYB",
        }
        for key, sym in needed.items():
            if key in result:  # 富邦已抓到的跳過
                continue
            try:
                h = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
                if h is None or len(h) < 2:
                    continue
                # ★ 修正：先丟掉 NaN 列，六個指標統一防呆（原本只有 twii 有做合理性檢查，
                #   nasdaq/sp500 抓到 NaN 時會直接被 jsonify 成不合法的 JSON，把整個 /api/state 弄壞）
                h = h.dropna(subset=["Close"])
                if len(h) < 2:
                    logger.warning(f"yfinance 大盤 {key}: 資料全為 NaN，略過")
                    continue
                p  = float(h["Close"].iloc[-1])
                pv = float(h["Close"].iloc[-2])
                if math.isnan(p) or math.isnan(pv) or p <= 0 or pv <= 0:
                    logger.warning(f"yfinance 大盤 {key}: 價格異常 p={p} pv={pv}，略過")
                    continue
                if key == "twii" and not (5000 < p < 30000):
                    continue
                if key == "tpex" and not (100 < p < 600):
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
                logger.warning(f"yfinance 大盤 {key}: {e}")

    # ── 富果(富邦)備援：yfinance 抓不到 twii/tpex 時，改用富果公開行情 API ──
    if "twii" not in result or "tpex" not in result:
        try:
            from fubon_broker import get_market_index_fubon
            fugle_idx = get_market_index_fubon()
            for key in ("twii", "tpex"):
                if key not in result and key in fugle_idx:
                    v = fugle_idx[key]
                    p, pv = v.get("price"), v.get("prev")
                    if p and pv and not (math.isnan(p) or math.isnan(pv)):
                        result[key] = v
                        logger.info(f"[Fallback] {key} 改用富果來源: {v.get('source')}")
        except Exception as e:
            logger.warning(f"富果備援大盤指數失敗: {e}")

    # ── 市場狀態判斷 ──
    if "twii" in result:
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

    return _cache_set("market_index", result)

# ════════════════════════════════════════════════
# 法人數據（TWSE 官方 API）
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
                code = row[0].strip()
                name = row[1].strip()
                def parse_int(v):
                    return int(v.replace(",", "").replace("+", "")) if v.strip() not in ("-", "") else 0
                foreign_net = parse_int(row[4]) if len(row) > 4 else 0
                trust_net   = parse_int(row[7]) if len(row) > 7 else 0
                total_net   = parse_int(row[11]) if len(row) > 11 else 0
                result[code] = {
                    "name":        name,
                    "foreign_net": foreign_net,
                    "trust_net":   trust_net,
                    "total_net":   total_net,
                    "signal": (
                        "strong_buy"  if foreign_net > 500 and trust_net > 0 else
                        "buy"         if foreign_net > 100 else
                        "strong_sell" if foreign_net < -500 else
                        "sell"        if foreign_net < -100 else "neutral"
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
        net_buy = int(latest[4].replace(",", "").replace("+", "")) if len(latest) > 4 else 0
        result  = {
            "date":          latest[0],
            "net_buy_twd":   net_buy,
            "net_buy_lots":  round(net_buy / 1000, 0),
            "signal": (
                "strong_buy"  if net_buy >  50e8 else
                "buy"         if net_buy >  0    else
                "strong_sell" if net_buy < -50e8 else "sell"
            ),
        }
        return _cache_set("foreign_total", result)
    except Exception as e:
        logger.error(f"fetch_foreign_total_flow: {e}")
        return {}

# ════════════════════════════════════════════════
# 市場總覽
# ════════════════════════════════════════════════
def fetch_market_overview() -> Dict:
    if c := _cache_get("market_overview", 1800):
        return c
    overview = {"fetched_at": datetime.now(timezone.utc).isoformat()}
    overview["index"]         = fetch_market_index()
    overview["foreign"]       = fetch_foreign_total_flow()
    overview["institutional"] = fetch_institutional_flow()
    overview["data_source"]   = "fubon_neo+yfinance+twse" if _fubon_logged else "yfinance+twse"

    score = 50
    idx   = overview["index"]
    if "twii" in idx:
        chg    = idx["twii"]["chg"]
        score += 15 if chg > 1.5 else 8 if chg > 0.5 else 3 if chg > 0 else -15 if chg < -1.5 else -8 if chg < -0.5 else -3
    if "vix" in idx:
        vix    = idx["vix"]["price"]
        score += 10 if vix < 15 else 5 if vix < 20 else -15 if vix > 30 else -8 if vix > 25 else 0
    fn     = overview.get("foreign", {}).get("net_buy_twd", 0)
    score += 15 if fn > 50e8 else 8 if fn > 10e8 else -15 if fn < -50e8 else -8 if fn < -10e8 else 0
    score  = max(0, min(100, score))

    overview["sentiment_score"] = score
    overview["sentiment_zh"]    = (
        "強烈看多" if score >= 80 else
        "偏多"     if score >= 60 else
        "中性"     if score >= 40 else
        "偏空"     if score >= 20 else "強烈看空"
    )
    overview["can_trade"]     = overview["index"].get("market_status", "normal") != "stop"
    overview["market_status"] = overview["index"].get("market_status", "normal")
    overview["is_trading"]    = _is_trading_session()
    return _cache_set("market_overview", overview)

# ════════════════════════════════════════════════
# 個股輔助
# ════════════════════════════════════════════════
def fetch_stock_institutional(code: str, date_str=None) -> Dict:
    return fetch_institutional_flow(date_str).get(code, {})

def get_market_session() -> Dict:
    now_tw    = datetime.now(timezone(timedelta(hours=8)))
    wd        = now_tw.weekday()
    total_min = now_tw.hour * 60 + now_tw.minute
    if wd >= 5:
        return {"session": "weekend", "session_zh": "週末休市", "is_open": False,
                "taipei_time": now_tw.strftime("%H:%M")}
    if 540 <= total_min <= 810:
        return {"session": "trading", "session_zh": "交易時段",
                "is_open": True, "taipei_time": now_tw.strftime("%H:%M"),
                "data_source": "fubon_neo" if _fubon_logged else "yfinance"}
    elif total_min < 540:
        return {"session": "pre_market", "session_zh": "盤前",
                "is_open": False, "taipei_time": now_tw.strftime("%H:%M")}
    else:
        return {"session": "after_market", "session_zh": "盤後",
                "is_open": False, "taipei_time": now_tw.strftime("%H:%M")}

def get_fubon_connection_status() -> Dict:
    """富邦連線狀態"""
    return {
        "sdk_available": FUBON_SDK_AVAILABLE,
        "connected":     _fubon_logged,
        "api_key_set":   bool(FUBON_API_KEY),
        "is_trading":    _is_trading_session(),
    }
