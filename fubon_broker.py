"""
fubon_broker.py — 富邦證券行情數據 v2.0（純查詢版）
只取行情數據，不涉及下單功能

富邦提供兩種免費數據管道：
1. Fubon Neo SDK（需帳戶）→ 即時 Tick、盤中 K 線
2. FubonSecurities 公開 API → 不需帳戶，直接用

本版優先用公開 API，有帳戶時自動升級到 Neo SDK
"""
import logging, requests, time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

import os
FUBON_API_KEY    = os.getenv("FUBON_API_KEY", "")
FUBON_API_SECRET = os.getenv("FUBON_API_SECRET", "")
FUBON_ACCOUNT    = os.getenv("FUBON_ACCOUNT", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

# ── 嘗試載入富邦 SDK（有帳戶才會成功）──
try:
    from fubon_neo.sdk import FubonSDK
    FUBON_SDK_OK = True
    logger.info("✅ 富邦 Neo SDK 載入成功")
except ImportError:
    FUBON_SDK_OK = False
    logger.info("富邦 Neo SDK 未安裝，改用公開 API")

# ══════════════════════════════════════════════
# 方案一：富邦公開行情 API（不需帳戶、不需 SDK）
# ══════════════════════════════════════════════
# 富邦公開行情端點（官方公開，無需金鑰）
FUBON_PUBLIC_BASE = "https://api.fugle.tw/marketdata/v1.0"  # 富果（富邦子公司）公開 API

# ── 富果 API（富邦旗下，免費方案即可用）──
FUGLE_API_KEY = os.getenv("FUGLE_API_KEY", "")  # 富果免費申請，與富邦帳戶分開


def fetch_fugle_quote(code: str) -> Dict:
    """
    富果 MarketData API — 即時報價
    申請網址：https://developer.fugle.tw
    免費方案：60次/分鐘，夠用
    code: 如 "2330"（不含 .TW）
    """
    if not FUGLE_API_KEY:
        return {"error": "FUGLE_API_KEY 未設定", "source": "fugle"}
    try:
        url = f"{FUBON_PUBLIC_BASE}/stock/intraday/quote/{code}"
        r = requests.get(url,
                         headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
                         timeout=8)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "source": "fugle"}
        d = r.json()
        return {
            "code":        code,
            "ticker":      f"{code}.TW",
            "name":        d.get("name", ""),
            "price":       float(d.get("closePrice", 0) or d.get("lastPrice", 0)),
            "open":        float(d.get("openPrice", 0)),
            "high":        float(d.get("highPrice", 0)),
            "low":         float(d.get("lowPrice", 0)),
            "prev_close":  float(d.get("previousClose", 0)),
            "chg":         float(d.get("change", 0)),
            "chg_pct":     float(d.get("changePercent", 0)),
            "volume_lots": int(d.get("volume", 0)) // 1000,
            "bid":         float(d.get("bid", 0)),
            "ask":         float(d.get("ask", 0)),
            "market_cap":  d.get("marketCapitalization"),
            "is_suspended":d.get("isSuspended", False),
            "source":      "fugle",
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning(f"fetch_fugle_quote {code}: {e}")
        return {"error": str(e), "source": "fugle"}


def fetch_fugle_candles(code: str, resolution: str = "D", count: int = 60) -> Dict:
    """
    富果 — K 線數據
    resolution: "1"=1分 "5"=5分 "D"=日線 "W"=週線
    """
    if not FUGLE_API_KEY:
        return {"error": "FUGLE_API_KEY 未設定"}
    try:
        params = {"resolution": resolution, "limit": count}
        r = requests.get(
            f"{FUBON_PUBLIC_BASE}/stock/historical/candles/{code}",
            headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
            params=params, timeout=10,
        )
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        d = r.json()
        candles = d.get("data", [])
        closes  = [float(c["close"])  for c in candles if c.get("close")]
        opens   = [float(c["open"])   for c in candles if c.get("open")]
        highs   = [float(c["high"])   for c in candles if c.get("high")]
        lows    = [float(c["low"])    for c in candles if c.get("low")]
        volumes = [int(c.get("volume", 0)) // 1000 for c in candles]
        dates   = [c.get("date","")[:10] for c in candles]
        return {
            "code": code, "resolution": resolution,
            "closes": closes, "opens": opens, "highs": highs,
            "lows": lows, "volumes": volumes, "dates": dates,
            "bar_count": len(closes),
            "current_price": closes[-1] if closes else 0,
            "source": "fugle",
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_fugle_market_overview() -> Dict:
    """
    富果 — 大盤指數（加權/上櫃）
    """
    if not FUGLE_API_KEY:
        return {}
    result = {}
    index_map = {
        "twii": "TAIEX",   # 加權指數
        "tpex": "TPEx",    # 上櫃指數
    }
    for key, symbol in index_map.items():
        try:
            r = requests.get(
                f"{FUBON_PUBLIC_BASE}/stock/intraday/quote/{symbol}",
                headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
                timeout=8,
            )
            if r.status_code == 200:
                d = r.json()
                p  = float(d.get("closePrice", 0) or d.get("lastPrice", 0))
                pv = float(d.get("previousClose", 0))
                result[key] = {
                    "price":   round(p, 2),
                    "prev":    round(pv, 2),
                    "chg":     round((p-pv)/pv*100, 2) if pv else 0,
                    "chg_pt":  round(p-pv, 2),
                    "source":  "fugle",
                }
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"fetch_fugle_market_overview {key}: {e}")
    return result


def fetch_fugle_top_movers(market: str = "TSE") -> List[Dict]:
    """
    富果 — 漲跌幅排行（漲停/跌停/成交量最大）
    market: "TSE"=上市  "OTC"=上櫃
    """
    if not FUGLE_API_KEY:
        return []
    try:
        r = requests.get(
            f"{FUBON_PUBLIC_BASE}/stock/intraday/movers",
            headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
            params={"market": market, "direction": "up", "limit": 20},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return [
            {
                "code":    item.get("symbol",""),
                "name":    item.get("name",""),
                "price":   float(item.get("closePrice", 0)),
                "chg_pct": float(item.get("changePercent", 0)),
                "volume":  int(item.get("volume",0)) // 1000,
            }
            for item in r.json().get("data", [])
        ]
    except Exception as e:
        logger.warning(f"fetch_fugle_top_movers: {e}"); return []


# ══════════════════════════════════════════════
# 方案二：富邦 Neo SDK 行情（有帳戶用）
# ══════════════════════════════════════════════

class FubonMarketData:
    """
    富邦 Neo SDK 行情查詢
    需要帳戶才能使用，比富果 API 有更高請求上限
    """
    def __init__(self):
        self._sdk    = None
        self._logged = False

    def connect(self) -> bool:
        if not FUBON_SDK_OK:
            return False
        if not all([FUBON_API_KEY, FUBON_API_SECRET]):
            return False
        try:
            self._sdk = FubonSDK()
            result = self._sdk.login(
                id=FUBON_API_KEY,
                trade_pass=FUBON_API_SECRET,
            )
            if result.is_success:
                self._logged = True
                logger.info("✅ 富邦 Neo SDK 行情連線成功")
                return True
            return False
        except Exception as e:
            logger.warning(f"富邦 Neo 連線失敗: {e}"); return False

    def get_quote(self, code: str) -> Dict:
        """即時報價（比富果更即時）"""
        if not self._logged: return {}
        try:
            result = self._sdk.marketdata.intraday.ticker(symbol=code)
            if result.is_success:
                d = result.data
                return {
                    "code":       code,
                    "price":      float(d.get("close", 0)),
                    "chg":        float(d.get("change", 0)),
                    "chg_pct":    float(d.get("change_percent", 0)),
                    "volume_lots":int(d.get("total_volume", 0)) // 1000,
                    "bid":        float(d.get("bid", 0)),
                    "ask":        float(d.get("ask", 0)),
                    "source":     "fubon_neo",
                }
        except Exception as e:
            logger.warning(f"Neo get_quote {code}: {e}")
        return {}

    def get_candles(self, code: str, timeframe: str = "D") -> Dict:
        """歷史 K 線"""
        if not self._logged: return {}
        try:
            result = self._sdk.marketdata.historical.candles(
                symbol=code, timeframe=timeframe
            )
            if result.is_success:
                bars = result.data or []
                return {
                    "closes":  [float(b.get("close",0)) for b in bars],
                    "opens":   [float(b.get("open",0))  for b in bars],
                    "highs":   [float(b.get("high",0))  for b in bars],
                    "lows":    [float(b.get("low",0))   for b in bars],
                    "volumes": [int(b.get("volume",0))//1000 for b in bars],
                    "bar_count": len(bars),
                    "source":  "fubon_neo",
                }
        except Exception as e:
            logger.warning(f"Neo get_candles {code}: {e}")
        return {}


# ══════════════════════════════════════════════
# 統一介面（自動選擇最佳來源）
# ══════════════════════════════════════════════

_fubon_md = FubonMarketData()
_fubon_connected = False


def get_realtime_quote(code: str) -> Dict:
    """
    取得即時報價
    優先順序：富邦 Neo SDK > 富果 API > yfinance
    """
    global _fubon_connected

    # 1. 富邦 Neo（有帳戶時）
    if FUBON_SDK_OK and FUBON_API_KEY and not _fubon_connected:
        _fubon_connected = _fubon_md.connect()
    if _fubon_connected:
        q = _fubon_md.get_quote(code)
        if q: return q

    # 2. 富果 API（有 FUGLE_API_KEY 時）
    if FUGLE_API_KEY:
        q = fetch_fugle_quote(code)
        if not q.get("error"): return q

    # 3. yfinance fallback
    try:
        import yfinance as yf
        h = yf.Ticker(f"{code}.TW").history(period="2d", interval="1d")
        if h is not None and len(h) >= 2:
            p  = float(h["Close"].iloc[-1])
            pv = float(h["Close"].iloc[-2])
            return {
                "code": code, "price": round(p,2),
                "chg": round(p-pv,2), "chg_pct": round((p-pv)/pv*100,2) if pv else 0,
                "volume_lots": int(h["Volume"].iloc[-1]) // 1000,
                "source": "yfinance",
            }
    except Exception as e:
        logger.warning(f"yfinance fallback {code}: {e}")

    return {"code": code, "error": "所有來源均失敗"}


def get_market_index_fubon() -> Dict:
    """
    大盤指數（富果優先）
    """
    if FUGLE_API_KEY:
        result = fetch_fugle_market_overview()
        if result: return result

    # fallback: yfinance
    try:
        import yfinance as yf
        result = {}
        for key, sym in {"twii":"^TWII","tpex":"^TPEX"}.items():
            h = yf.Ticker(sym).history(period="5d",interval="1d")
            if h is not None and len(h) >= 2:
                p=float(h["Close"].iloc[-1]); pv=float(h["Close"].iloc[-2])
                result[key]={"price":round(p,2),"chg":round((p-pv)/pv*100,2),"source":"yfinance"}
        return result
    except:
        return {}
