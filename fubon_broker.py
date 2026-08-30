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

# ★ 修正：跟 data_fetcher.py 同一個問題——period fallback(5d→1mo→3mo)拿到的資料
#   如果不驗證日期，可能是很久以前的舊資料卻被當成當下報價顯示，比顯示空白更危險。
#   這裡獨立複製一份同樣的新鮮度檢查（不 import data_fetcher，避免兩個檔案互相 import）。
def _last_bar_is_fresh(h, max_age_days: int = 7) -> bool:
    try:
        last_ts = h.index[-1]
        if getattr(last_ts, "tzinfo", None) is not None:
            last_dt = last_ts.tz_convert("UTC").to_pydatetime().replace(tzinfo=None)
        else:
            last_dt = last_ts.to_pydatetime()
        age = datetime.utcnow() - last_dt
        return age.total_seconds() <= max_age_days * 86400
    except Exception:
        return True

# ★ 修正：2026-08-30——跟 data_fetcher.py 同一個問題、同一個修法（獨立複製一份，
#   避免兩個檔案互相 import）：個股歷史 K 線在 Render 上連區間退避都救不回來，懷疑是
#   Yahoo 對雲端機房 IP 的 bot 防護擋掉了預設的 requests session。用 curl_cffi 偽裝
#   成真實 Chrome 的 TLS/HTTP 指紋建立 session。老實講這是「值得一試」不是「保證有效」。
def _get_yf_session():
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests.Session(impersonate="chrome")
    except Exception as e:
        logger.warning(f"curl_cffi session 建立失敗，改用 yfinance 預設 session：{e}")
        return None

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
    富果 — K 線數據（歷史 K 線 REST API）
    ★ 修正：2026-08-30——追查「2382 回測一直卡在歷史數據不足（需60根日線），
    log 卻完全沒有任何新訊息」的過程中，對照富果官方文件
    （https://developer.fugle.tw/docs/data/http-api/historical/candles/）才發現
    這裡打的查詢參數整個是錯的，而且是「錯了但不會報錯」的那種最難抓的錯：
      1. 官方參數叫 timeframe，這裡原本送的是 resolution——富果 API 根本不認得
         resolution 這個名字，會被直接忽略，剛好用了官方預設值 "D"（日線），
         所以看起來「還能動」，掩蓋了問題。
      2. 官方歷史 K 線是用 from/to 日期區間取資料，根本沒有 limit/count 這個
         參數——原本送的 limit=count 一樣被完全忽略。from 預設是「一個月前」、
         to 預設是「今天」，所以不管呼叫端要求 60 根還是 250 根，實際上永遠
         只會拿到「最近一個月」的日線，大概 15~22 根交易日，剛好落在
         _fugle_get_candles() 自己 len(closes)<20 的門檻附近，時有時無——
         這就是回測一直不夠 60 根的真正原因：不是富果沒有資料，是我們自己
         的參數名稱送錯，讓 API 用預設值回應了一小段「看起來正常、但完全
         不夠用」的資料，HTTP 200 成功、沒有任何 error 欄位，從回應本身
         完全看不出問題出在哪。
      3. 官方文件寫 sort 預設是 desc（新到舊），但這個函式一直都假設資料是
         「舊到新」排序——closes[-1] 被當成「目前價格」使用。如果富果真的
         照文件預設用 desc 回傳，current_price／prev_close／change_pct 全部
         會算反，均線、RSI 等技術指標也會全部算錯，比「根數不夠」更嚴重，
         是資料正確性問題而不只是資料量問題。這裡直接明確送 sort=asc，
         不依賴任何預設值猜測。
    resolution: "D"=日線 "W"=週線 "60"=60分K（對應官方 timeframe 參數值，
                這幾個值本身沒錯，只是原本用錯參數名稱送出去，白白浪費）
    count: 至少要拿到幾根資料，用來換算 from/to 要往前抓多少天當緩衝
    """
    if not FUGLE_API_KEY:
        return {"error": "FUGLE_API_KEY 未設定"}
    try:
        # ★ 修正：2026-08-30（緊接稍早同一天的參數修正，部署後在 Render log 才發現）——
        #   上一輪把 resolution/limit 改成 timeframe/from/to 之後，日線區間算出來
        #   455 天、週線算出來甚至超過 1600 天，結果部署上線後每一次呼叫都收到
        #   HTTP 400「Date range must be less than one year」——富果歷史K線 API
        #   還有一個官方文件沒寫清楚的硬性限制：from~to 區間不能超過 365 天。
        #   這個限制之前完全沒考慮到，等於「參數名稱修對了，但區間長度又送錯」，
        #   每一次呼叫都保證會被拒絕，白白浪費一次 API 額度（免費方案 60次/分鐘，
        #   全市場掃描很容易被這種註定失敗的呼叫拖累）。雖然有 fallback 到
        #   yfinance，不是「完全失敗、抓不到資料」，但這個浪費本身不應該存在，
        #   而且會讓人誤以為「富果又壞了」，這裡直接修正：
        #     - 日線(D)：359 天（留一點緩衝，不卡在剛好 365 那條線上）大約可以
        #       拿到 230~250 根交易日，backtest 要的 60 根、indicators 要的
        #       125 根都夠。
        #     - 週線(W)：359 天只能拿到約 50 根週線，本身就低於 indicators 要的
        #       125 根——這裡選擇不做「多次請求串接湊足根數」，因為那樣一檔
        #       股票的一次週線請求就要打好幾次富果 API，全市場掃描的免費額度
        #       撐不住。直接讓這個結果在 fetch_ohlcv() 既有的「根數不夠就不
        #       快取、改用 yfinance」邏輯下自然 fallback——yfinance 那邊本來就
        #       有到 5y/max 的週線區間退避，足以補齊，等於週線這個時框富果只
        #       當「加速」用，真正的資料量還是靠 yfinance 撐，不是這裡沒改好。
        #     - 60分K：本來就只抓 90 天上下，沒有超過 365 天限制，不受影響。
        today = datetime.now(timezone.utc).date()
        if resolution in ("D", "W"):
            lookback_days = 359
        else:
            lookback_days = max(90, int(count / 3) + 20)       # 60分K：一個交易日約 4-5 根，抓寬一點當緩衝（假日、缺量交易日都會拉低平均根數）
        from_date = (today - timedelta(days=lookback_days)).isoformat()
        to_date   = today.isoformat()
        params = {
            "timeframe": resolution,
            "from":      from_date,
            "to":        to_date,
            "sort":      "asc",
        }
        r = requests.get(
            f"{FUBON_PUBLIC_BASE}/stock/historical/candles/{code}",
            headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
            params=params, timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"fetch_fugle_candles {code}({resolution}) {from_date}~{to_date}: HTTP {r.status_code} - {r.text[:200]}")
            return {"error": f"HTTP {r.status_code}"}
        d = r.json()
        candles = d.get("data", [])
        if not candles:
            logger.warning(f"fetch_fugle_candles {code}({resolution}) {from_date}~{to_date}: HTTP 200 但 data 是空的，回應：{str(d)[:200]}")
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
            else:
                # ★ 修正：2026-08-30——這裡原本非 200 時完全不記錄任何東西，
                #   跟今天抓到的好幾個「靜默失敗」是同一個問題模式。不管是金鑰打錯(401)、
                #   額度用完(403)，還是 TAIEX/TPEx 這兩個指數代碼本身不被富果 API 接受(404)，
                #   之前都只會讓這個函式安靜地跳過、整包回傳空字典，完全看不出真正原因。
                logger.warning(f"fetch_fugle_market_overview {key}({symbol}): HTTP {r.status_code} - {r.text[:200]}")
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"fetch_fugle_market_overview {key}: {e}")
    return result


def fugle_key_diagnostic() -> Dict:
    """
    ★ 新增：2026-08-30——專門給 /api/diagnostics 用的富果金鑰檢測。
    跟 fetch_fugle_market_overview() 分開寫的原因：那個函式測的是 TAIEX/TPEx 這兩個
    指數代碼，如果富果的 API 剛好不接受這兩個代碼當作 symbol(而不是金鑰本身有問題)，
    會誤導使用者以為金鑰壞掉。這裡改用「2330」(台積電，最沒有爭議的真實個股代碼)
    單獨測一次，把金鑰本身有沒有效跟「指數代碼對不對」這兩件事分開檢查，並且把
    實際的 HTTP 狀態碼、回應內容都回傳出來，不再是黑盒子。
    """
    if not FUGLE_API_KEY:
        return {"ok": False, "detail": "未設定 FUGLE_API_KEY"}
    try:
        r = requests.get(
            f"{FUBON_PUBLIC_BASE}/stock/intraday/quote/2330",
            headers={**HEADERS, "X-API-KEY": FUGLE_API_KEY},
            timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            price = d.get("closePrice") or d.get("lastPrice")
            if price:
                return {"ok": True, "detail": f"金鑰有效，測試查詢 2330 拿到價格 {price}"}
            return {"ok": False, "detail": f"HTTP 200 但回應內容不含價格欄位，可能是 API 回應格式跟預期不同：{r.text[:200]}"}
        if r.status_code == 401:
            return {"ok": False, "detail": "HTTP 401 未授權——金鑰本身無效（打錯、過期，或還沒審核通過）"}
        if r.status_code == 403:
            return {"ok": False, "detail": "HTTP 403 禁止存取——金鑰有效但沒有這個資源的權限，或額度已用完"}
        if r.status_code == 429:
            return {"ok": False, "detail": "HTTP 429 請求過於頻繁——額度限制，稍後再試"}
        return {"ok": False, "detail": f"HTTP {r.status_code}：{r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "detail": f"連線本身失敗：{e}"}


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
        _sess = _get_yf_session()
        h = (yf.Ticker(f"{code}.TW", session=_sess) if _sess else yf.Ticker(f"{code}.TW")).history(period="2d", interval="1d")
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
    # ★ 修正兩個問題：
    #   1. "^TPEX" 不是有效代碼（Yahoo 回 404 "Quote not found"，已用 WebFetch 查證台股上櫃
    #      指數在 Yahoo 正確代碼是 "^TWOII"），改對之後這條路徑才真的抓得到上櫃指數。
    #   2. 這裡回傳的 dict 原本沒有 "prev" 欄位，但 data_fetcher.py 呼叫這個函式當備援時
    #      是用 v.get("prev") 做防呆檢查——沒有 "prev" 代表這條備援路徑就算資料抓到了，
    #      也永遠會被 data_fetcher.py 的防呆邏輯判定為「沒抓到」而丟棄，形同虛設。
    try:
        import yfinance as yf
        result = {}
        _sess = _get_yf_session()
        # ★ 修正：跟 data_fetcher.py 同一個問題——2026-08-29 實測 ^TWOII 用 period="5d"
        #   會持續性失敗（連續 3 小時以上每次都失敗，^TWII 卻正常），這裡是這條備援路徑
        #   最後的機會，一樣加上 period fallback，不然 tpex 永遠拿不到資料。
        for key, sym in {"twii":"^TWII","tpex":"^TWOII"}.items():
            h = None
            for fallback_period in ("5d", "1mo", "3mo"):
                t = yf.Ticker(sym, session=_sess) if _sess else yf.Ticker(sym)
                candidate = t.history(period=fallback_period, interval="1d")
                if candidate is not None and len(candidate) >= 2 and _last_bar_is_fresh(candidate):
                    h = candidate
                    break
            if h is not None and len(h) >= 2:
                p=float(h["Close"].iloc[-1]); pv=float(h["Close"].iloc[-2])
                result[key]={"price":round(p,2),"prev":round(pv,2),"chg":round((p-pv)/pv*100,2),"source":"yfinance"}
        return result
    except:
        return {}
