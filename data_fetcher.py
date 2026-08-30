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

# ★ 修正：2026-08-30 實測發現，就算加了區間退避(1y→2y→5y),個股(2382.TW 等)還是
#   完全抓不到任何一根日線資料(不是「根數不夠」,是每個區間都直接拿不到東西,連
#   yfinance 自己的錯誤訊息都沒印,代表 Yahoo 端連資料都沒回,不是資料被截斷)。
#   這跟已知的 Yahoo 對雲端/機房 IP 做 bot 防護、擋掉沒有瀏覽器指紋的請求是同一個
#   已知問題模式(yfinance 官方 issue 上大量雲端部署案例都回報過)。改法：用
#   curl_cffi 偽裝成真實 Chrome 瀏覽器的 TLS/HTTP 指紋建立 session,交給
#   yf.Ticker(..., session=...) 用,取代 yfinance 預設的 requests session。
#   老實講：這是「有實證支持、值得一試」的修正,不是保證有效——我沒辦法從這個
#   sandbox 直接連 Yahoo 驗證 Render 上的請求是不是真的因為這個原因被擋,部署後
#   還是要看個股回測是否恢復正常。就算這個也沒用,FUGLE_API_KEY 是唯一保證有效的路。
try:
    from curl_cffi import requests as _cffi_requests
    _YF_SESSION = _cffi_requests.Session(impersonate="chrome")
except Exception as _e:
    _YF_SESSION = None
    logger.warning(f"curl_cffi session 建立失敗，改用 yfinance 預設 session：{_e}")

def _yf_ticker(symbol: str):
    """統一建立 yf.Ticker，優先用偽裝瀏覽器指紋的 session（curl_cffi 不可用時退回預設）"""
    if _YF_SESSION is not None:
        try:
            return yf.Ticker(symbol, session=_YF_SESSION)
        except Exception as e:
            logger.warning(f"yf.Ticker({symbol}) 使用 curl_cffi session 失敗，改用預設：{e}")
    return yf.Ticker(symbol)

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

# ★ 修正：這份檔案原本假設「填了 FUBON_API_KEY/SECRET 就能連上富邦 Neo SDK」，
#   但富邦新一代 API 官方文件（fbs.com.tw/TradeAPI）明確兩件事：
#   1. 富邦 Neo SDK 只發行 Windows 的 .whl（不在 PyPI 上，要去富邦官網手動下載），
#      Render 這種 Linux 容器裝不了這個套件（下面 FUBON_SDK_AVAILABLE 在這裡恆為 False）。
#   2. 它的登入方式是「身分證字號＋交易密碼＋本機憑證檔＋憑證密碼」，不是 API Key/Secret
#      這種模式──所以 FUBON_API_KEY/SECRET 這組環境變數，不管填什麼，都不可能讓
#      這一段真的連得上富邦，這是原始設計的假設就有問題，不是憑證填錯。
#   下面的 _fubon_get_quote()/_fubon_get_candles() 保留著（不會報錯，純粹是安全的無效分支），
#   真正能在 Render 上運作的即時/歷史資料來源改成富果(Fugle)──它是純 HTTP API，
#   不需要 SDK、不需要憑證檔，只要申請免費的 FUGLE_API_KEY 就能用（見 fubon_broker.py）。
FUGLE_API_KEY = os.getenv("FUGLE_API_KEY", "")

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

# ★ 修正：2026-08-29 實測發現，前面那個 period fallback(5d→1mo→3mo)雖然讓
#   ^TWOII(上櫃指數)不再整個抓不到，但拿到的數字是錯的——用 WebFetch 直接比對
#   Yahoo 網頁版即時報價，我們抓到的「現價/前一日」跟 Yahoo 網頁版顯示的完全對不上，
#   落差達 -7%，明顯是 3mo 這個較長區間裡「最後兩根」其實是很久以前的舊資料，
#   不是真正最新的收盤。一個看起來像即時報價、實際上是幾週前舊資料的數字，
#   比直接顯示「—」空白更危險——使用者會誤以為這是當下真實行情。
#   加一層新鮮度檢查：不管哪個區間抓到的，都要確認「最後一根」的日期夠新
#   （容許週末+假日，7 天內），太舊就當作沒抓到，不要顯示看起來像即時卻是舊的數字。
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
        return True  # 判斷不出日期就不擋，回到原本「有拿到就算數」的行為

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
# 富果(Fugle) 即時報價／歷史K線
# ★ 新增：這是這次真正能在 Render 上運作的即時/歷史資料來源。
#   fetch_fugle_quote()/fetch_fugle_candles() 是純 HTTP 呼叫（定義在 fubon_broker.py），
#   不需要 SDK、不需要憑證檔，設定 FUGLE_API_KEY（免費申請，見說明文件）就會生效。
# ════════════════════════════════════════════════
def _fugle_get_quote(code: str) -> Optional[Dict]:
    """富果即時報價（盤中用）"""
    if not FUGLE_API_KEY:
        return None
    try:
        from fubon_broker import fetch_fugle_quote
        q = fetch_fugle_quote(code)
        price = q.get("price", 0)
        if q.get("error") or not price or price <= 0:
            return None
        return {"code": code, "price": round(price, 2), "source": "fugle"}
    except Exception as e:
        logger.warning(f"富果 get_quote {code}: {e}")
        return None

def _fugle_get_candles(ticker: str, code: str, tf_key: str, bars: int) -> Optional[Dict]:
    """
    富果歷史 K 線。
    ★ resolution 對照（"D"=日線 "W"=週線，小時線用 "60"=60分K）是照 fubon_broker.py
    既有的 docstring 對照表推的，這個 sandbox 沒有真的金鑰沒辦法實際打一次驗證，
    拿到金鑰之後建議看一下小時線的 bar_count 有沒有正常回來（回測頁或 log 都看得到）。
    """
    if not FUGLE_API_KEY:
        return None
    try:
        from fubon_broker import fetch_fugle_candles
        res_map = {"daily": "D", "weekly": "W", "hourly": "60"}
        d = fetch_fugle_candles(code, resolution=res_map.get(tf_key, "D"), count=bars)
        closes = d.get("closes") or []
        if d.get("error"):
            logger.warning(f"富果 get_candles {code} {tf_key}: {d.get('error')}")
            return None
        # ★ 修正：2026-08-30——這裡原本只要 >=20 根就算成功，回到 fetch_ohlcv() 就會被
        #   直接快取 1 小時（見下方 fetch_ohlcv() 的修正說明）；而且不管是「根數不夠」還是
        #   「根本沒資料」都完全不記錄，是今天抓到的第 N 個靜默失敗。20 根這個門檻本身沒錯
        #   （用來擋掉完全異常的空資料），但一定要留下 log，不然沒辦法從 Render log 判斷
        #   富果那邊到底回了多少根、是不是又是參數問題。
        if len(closes) < 20:
            logger.warning(f"富果 get_candles {code} {tf_key}: 只拿到 {len(closes)} 根，低於最低可用門檻(20)，視為失敗")
            return None
        tf = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])
        return {
            "ticker":        ticker,
            "tf_key":        tf_key,
            "label":         tf.get("label", tf_key),
            "closes":        closes,
            "opens":         d.get("opens", []),
            "highs":         d.get("highs", []),
            "lows":          d.get("lows", []),
            "volumes":       d.get("volumes", []),
            "dates":         d.get("dates", []),
            "current_price": closes[-1],
            "prev_close":    closes[-2] if len(closes) > 1 else closes[-1],
            "change_pct":    round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) > 1 else 0,
            "bar_count":     len(closes),
            "source":        "fugle",
        }
    except Exception as e:
        logger.warning(f"富果 get_candles {code} {tf_key}: {e}")
        return None

# ════════════════════════════════════════════════
# 主要 K 線抓取（互補邏輯）
# ════════════════════════════════════════════════
def fetch_ohlcv(ticker: str, tf_key: str = "daily") -> Optional[Dict]:
    """
    OHLCV 數據抓取。
    ★ 修正：優先順序改成「富果(Fugle) → 富邦 Neo(Render 上恆為無效，見檔頭說明) → yfinance」。
    ticker: 如 "2330.TW"
    """
    cache_k = f"ohlcv_{ticker}_{tf_key}"
    if c := _cache_get(cache_k, SYSTEM["cache_ttl_sec"]):
        return c

    code = ticker.replace(".TW", "").replace(".TWO", "")
    tf   = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])

    # ── 1. 盤中：嘗試富果即時 ──
    if _is_trading_session() and tf_key in ("daily", "hourly"):
        fugle_q = _fugle_get_quote(code)
        if fugle_q:
            # 盤中即時：只有最新一根，其餘用 yfinance 補歷史
            yf_data = _fetch_yf_ohlcv(ticker, tf_key)
            if yf_data:
                yf_data["closes"][-1]    = fugle_q["price"]
                yf_data["current_price"] = fugle_q["price"]
                yf_data["source"]        = "fugle+yfinance"
                _cross_validate(fugle_q["price"], yf_data.get("prev_close", fugle_q["price"]), ticker)
                return _cache_set(cache_k, yf_data)

    # ── 2. 盤中：嘗試富邦 Neo 即時（Render 上恆為無效分支，見檔頭說明，保留無害）──
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

    # ── 3. 富果歷史 K 線 ──
    if FUGLE_API_KEY:
        fugle_bars = _fugle_get_candles(ticker, code, tf_key, tf.get("bars", 250))
        if fugle_bars:
            # ★ 修正：2026-08-30——這裡原本不管根數夠不夠，只要 fugle_bars 不是 None/空
            #   就直接整包快取 1 小時，跟下面第 5 步 yfinance 那個「快取污染」是同一種
            #   bug、同一天發生兩次。今天查到的根本原因是 fetch_fugle_candles() 打的
            #   API 參數本身就是錯的（見 fubon_broker.py 修正說明），修好之後這裡應該
            #   能一次拿到足夠的根數；但即使修好了，還是保留這層「根數不夠就不快取、
            #   繼續往下一個來源試」的防呆，不要重蹈覆轍——上游來源難免還是會有臨時
            #   異常（額度用完、股票剛上市沒多久等）。
            bar_count = fugle_bars.get("bar_count", len(fugle_bars.get("closes", [])))
            if bar_count < 60:
                logger.warning(f"fetch_ohlcv {ticker}/{tf_key}: 富果只拿到 {bar_count} 根，低於可用門檻，不快取、改嘗試其他來源")
            else:
                return _cache_set(cache_k, fugle_bars)

    # ── 4. 富邦歷史 K 線（有帳戶時；Render 上恆為無效分支，見檔頭說明，保留無害）──
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

    # ── 5. yfinance 備用 ──
    yf_data = _fetch_yf_ohlcv(ticker, tf_key)
    if yf_data:
        # ★ 修正：2026-08-30 追查「2382 回測明明改了程式碼、log 卻完全沒有動靜」時抓到的
        #   真正原因——這裡原本不管根數夠不夠，只要 yf_data 不是 None 就整包快取 1 小時
        #   (SYSTEM["cache_ttl_sec"]=3600)。代表只要有「一次」拿到不夠用的根數(例如 30、
        #   40 根，回測需要 60 根)，就會被快取起來，接下來整整一小時內，不管程式碔本身
        #   怎麼修、Yahoo 那邊資料是不是已經恢復正常，都只會直接回傳這份不夠用的舊快取，
        #   完全不會再呼叫 _fetch_yf_ohlcv()——這也是為什麼我這次在 Render log 裡完全找
        #   不到任何新的診斷 log：根本沒有真的重新抓一次。改法：根數明顯不足以支撐回測
        #   (低於 60 根)時，這次結果還是回傳給呼叫者用，但不寫入快取，讓下一次呼叫可以
        #   重新嘗試，而不是被綁死一小時。
        bar_count = yf_data.get("bar_count", len(yf_data.get("closes", [])))
        if bar_count < 60:
            logger.warning(f"fetch_ohlcv {ticker}/{tf_key}: 只拿到 {bar_count} 根，低於可用門檻，不寫入快取（下次會重新嘗試，不會被卡住一小時）")
            return yf_data
        return _cache_set(cache_k, yf_data)

    logger.warning(f"fetch_ohlcv {ticker}/{tf_key}: 所有來源失敗")
    return None


def _fetch_yf_ohlcv(ticker: str, tf_key: str) -> Optional[Dict]:
    """yfinance K 線抓取"""
    if not YFINANCE_OK:
        return None
    tf = TIMEFRAMES.get(tf_key, TIMEFRAMES["daily"])
    # ★ 修正：2026-08-29 實測發現，連 2330(台積電，全市場流動性最好的股票)用
    #   period=tf["period"]（daily 是 "1y"）都抓不滿 60 根日線，回測直接報「歷史數據不足」。
    #   跟大盤指數的 ^TWOII 問題是同一種模式：不是這檔股票真的沒歷史資料，是 Yahoo 對
    #   「這次查詢的區間長度」回應不完整/被截斷。比照大盤指數的做法加一層區間退避：
    #   原本設定的區間根數不夠，就依序改試更長的區間，最後還是只取 tf["bars"] 根。
    _period_fallbacks = {
        "daily":  [tf["period"], "2y", "5y"],
        "weekly": [tf["period"], "10y", "max"],
        "hourly": [tf["period"], "180d", "730d"],  # yfinance 對 1h 區間上限是 730 天
    }.get(tf_key, [tf["period"]])
    periods, _seen = [], set()
    for p in _period_fallbacks:
        if p not in _seen:
            periods.append(p); _seen.add(p)
    try:
        # ★ 修正：原本的「>=20 根就停」門檻對「要不要再試更長區間」來說太寬鬆——
        #   23 根就會滿足 >=20，直接停在明顯不夠用的根數（indicators.py 要 125 根、
        #   backtester.py 要 60 根起跳），根本不會真的去試 2y/5y。改成以 130 根
        #   （backtester.py 自己的 LOOKBACK 門檻，略高於 indicators.py 需要的 125）
        #   當作「夠了、可以停」的目標，同時保留每個區間都嘗試過、取根數最多的那個，
        #   萬一連最長區間都不到 130 根（例如真的是剛上市沒多久的股票），
        #   至少回傳目前能拿到最多根數的那份，而不是直接放棄。
        target_bars = max(20, min(130, tf.get("bars", 130)))
        h = None
        _attempt_log = []
        for p in periods:
            candidate = _yf_ticker(ticker).history(period=p, interval=tf["interval"], auto_adjust=True)
            _attempt_log.append(f"{p}={0 if candidate is None else len(candidate)}根")
            if candidate is None or len(candidate) < 20:
                continue
            if h is None or len(candidate) > len(h):
                h = candidate
            if len(h) >= target_bars:
                break
        if h is None or len(h) < 20:
            # ★ 修正：2026-08-30 加上診斷 log——原本這裡完全靜默(不拋例外就不會印任何東西)，
            #   導致 2382.TW 回測失敗時 Render log 裡完全查不到任何線索。現在把每個區間
            #   實際拿到的根數都印出來，下次再失敗就能直接看出是「完全拿不到資料」還是
            #   「有資料但根數不夠」，區分是 Yahoo 網路層擋掉還是單純這檔股票歷史短。
            logger.warning(f"_fetch_yf_ohlcv {ticker}/{tf_key}: 所有區間都失敗（{', '.join(_attempt_log)}）")
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
                    if 5000 < p < 100000:
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
            # ★ 修正："^TPEX" 在 Yahoo Finance/yfinance 上查不到（HTTP 404 "Quote not found"，
            #   從你 Render 上的實際 log 抓到的錯誤），台股上櫃(TPEx)指數在 Yahoo 的正確代碼是
            #   "^TWOII"（櫃檯指數），已用 WebFetch 實際查證 tw.stock.yahoo.com 現在報價正常。
            "tpex":   "^TWOII",
            "sp500":  "^GSPC",
            "nasdaq": "^NDX",
            "vix":    "^VIX",
            "dxy":    "DX-Y.NYB",
        }
        for key, sym in needed.items():
            if key in result:  # 富邦已抓到的跳過
                continue
            try:
                # ★ 修正：2026-08-29 實測發現 ^TWOII 用 period="5d" 打 yfinance 會持續性失敗
                #   （不是偶發——連續追蹤超過 3 小時，每一次都噴
                #   "$^TWOII: possibly delisted; no price data found (period=5d)"，
                #   同一個迴圈裡的 ^TWII/^GSPC/^NDX/^VIX/DX-Y.NYB 用同樣的 period="5d" 卻完全正常，
                #   代表這是 Yahoo 對「這一個 symbol + 這個 period」組合本身的問題，不是限流)。
                #   加一層 period fallback：5d 拿不到資料就依序改試更長的區間再取最後兩根，
                #   便宜、無副作用（只有前一個區間真的失敗才會多打一次），但誠實講：這是防禦性補強，
                #   不是根治——我沒辦法從這裡直接連 Yahoo 驗證這樣改了以後 ^TWOII 是不是真的會成功，
                #   部署後要看 log 還有沒有再出現 ^TWOII 的 404/possibly delisted。真正不依賴 Yahoo
                #   的解法還是把 FUGLE_API_KEY 設定好（見說明文件）。
                # ★ 修正：加上新鮮度檢查——區間越長，越可能是舊資料，5d/1mo 都失敗、
                #   要退到 3mo 才拿到東西時，必須先確認最後一根真的是近期交易日，
                #   不然寧可維持原本「抓不到、顯示空白」的行為，也不要顯示錯誤的舊數字。
                h = None
                _attempt_log = []
                for fallback_period in ("5d", "1mo", "3mo"):
                    candidate = _yf_ticker(sym).history(period=fallback_period, interval="1d", auto_adjust=True)
                    _rows = 0 if candidate is None else len(candidate)
                    _attempt_log.append(f"{fallback_period}={_rows}根")
                    if candidate is not None and len(candidate) >= 2 and _last_bar_is_fresh(candidate):
                        h = candidate
                        break
                if h is None or len(h) < 2:
                    logger.info(f"yfinance 大盤 {key}({sym}): 所有區間都失敗或不夠新鮮（{', '.join(_attempt_log)}）")
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
                # ★ 修正：2026-08-30 用單元測試抓到的真實 bug——這兩個合理性上限是舊的
                #   （加權指數早就漲破 30000，現在(2026年)在 45000~46000 這個區間，
                #   原本 "p < 30000" 這個檢查會把「真實、新鮮」的加權指數資料當成異常值
                #   直接丟棄，逼系統每次都要繞去 fubon_broker 的備援路徑才拿得到資料——
                #   備援路徑剛好沒有做這個上限檢查，所以線上看起來「正常」，但這代表
                #   yfinance 主要路徑其實從來沒有真的成功過，一直在做多餘、有風險的重試。
                #   上限拉高、留足未來成長空間，不要再卡到會員自然上漲的合理股價。
                if key == "twii" and not (5000 < p < 100000):
                    logger.warning(f"yfinance 大盤 twii: 價格 {p} 超出合理性檢查範圍，略過")
                    continue
                if key == "tpex" and not (100 < p < 2000):
                    logger.warning(f"yfinance 大盤 tpex: 價格 {p} 超出合理性檢查範圍，略過")
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
