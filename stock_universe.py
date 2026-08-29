"""
stock_universe.py — 台股全市場品種管理 v1.1（無 pandas 版）
移除 pandas 依賴，改用純 Python list/dict
"""
import os, time, logging, requests, json
from typing import List, Dict, Optional
from config import SIGNAL_THRESHOLDS as THRESH, SYSTEM

logger = logging.getLogger(__name__)

CACHE_PATH = "instance/stock_universe.json"
CACHE_TTL  = 86400

TWSE_LIST_URL   = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_LIST_URL   = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TWSE_INFO_URL   = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_PUNISH_URL = "https://www.twse.com.tw/announcement/punish?response=json"   # 集中市場公布處置股票
TWSE_NOTICE_URL = "https://www.twse.com.tw/announcement/notice?response=json"   # 集中市場當日公布注意股票

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ★ 修正：原本用股票「名稱」比對這些關鍵字來排除注意股/處置股/全額交割股，
#   但 TWSE STOCK_DAY_ALL 的 Name 欄位只是單純的公司名稱，不會帶這類註記，
#   這個關鍵字過濾實際上幾乎不會比對到任何東西。保留關鍵字表當作最後一層防呆，
#   但真正的過濾改成用 TWSE 官方「處置」「注意」名單的證券代號（見 BLACKLIST_CODES）。
BLACKLIST_KEYWORDS = ["全額交割", "處置股票", "注意股票", "下市", "停止買賣"]
# ★ 修正：這個集合原本宣告了但從沒有任何地方寫入過，等於形同虛設。
#   現在由 _fetch_disposal_and_attention_codes() 在 build_universe() 時真正填入。
BLACKLIST_CODES = set()

def _fetch_disposal_and_attention_codes() -> set:
    """
    抓 TWSE 官方「公布處置股票」(punish) + 「當日公布注意股票」(notice) 的即時名單，
    回傳證券代號集合。這是台股全市場都掃時，排除波動最極端族群的關鍵一步。
    限制：這兩支是上市(TSE)專屬的官方 JSON API，上櫃(TPEX)目前沒有對應的公開端點，
    所以上櫃股票目前無法用同樣方式過濾（TPEX 官網的處置查詢是表單送出，不是 JSON API）。
    """
    codes = set()
    for url, label in [(TWSE_PUNISH_URL, "處置"), (TWSE_NOTICE_URL, "注意")]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                logger.warning(f"{label}股票清單 HTTP {r.status_code}")
                continue
            data = r.json()
            if data.get("stat") != "OK":
                continue
            fields = data.get("fields", [])
            if "證券代號" not in fields:
                logger.warning(f"{label}股票清單欄位格式異常: {fields}")
                continue
            code_idx = fields.index("證券代號")
            n = 0
            for row in data.get("data", []):
                try:
                    code = str(row[code_idx]).strip()
                    if code:
                        codes.add(code); n += 1
                except (IndexError, ValueError):
                    continue
            logger.info(f"TWSE {label}股票：{n} 檔")
        except Exception as e:
            logger.warning(f"_fetch_disposal_and_attention_codes ({label}): {e}")
    return codes

SECTOR_SCAN_PRIORITY = {
    "半導體": 1, "AI概念": 1, "電腦及週邊設備": 2,
    "電子零組件": 2, "電機機械": 2, "電力設備": 2,
    "光電": 3, "通信網路": 3, "資訊服務": 3,
    "其他電子": 3, "數位雲端": 3,
    "金融保險": 4, "生技醫療": 4, "航運": 4,
    "ETF": 1, "鋼鐵": 5, "化學": 5, "塑膠": 5,
    "建築營造": 6, "食品": 6, "紡織纖維": 6, "其他": 9,
}

def _is_cache_valid():
    if not os.path.exists(CACHE_PATH): return False
    return time.time() - os.path.getmtime(CACHE_PATH) < CACHE_TTL

# ★ 修正：原本用 `len(code) not in (4,5) or not code.isdigit()` 判斷「有效代碼」，
#   這個規則把台股所有槓桿/反向/主動式 ETF（代碼結尾帶一碼英文字母，如 00631L 元大台灣50正2、
#   00632R 元大台灣50反1、00400A 主動國泰動能高息）跟所有 6 碼數字 ETF（如 006208 元大台灣50、
#   006203 元大MSCI台灣）整批排除在外——這些不是冷門標的，00631L/00632R 是台股散戶交易量數一數二
#   大的商品。用 WebFetch 直接打 TWSE 官方 STOCK_DAY_ALL API 現場確認：006208、00631L 兩個代碼
#   都真的存在於官方回傳資料裡，代表這不是資料源沒有、是這裡的過濾規則把它們濾掉了。
#   同時原本的 `is_etf = len(code)==5` 也是錯的：0050、0056、0061 這些最老牌、交易量最大的 ETF
#   都是 4 碼，會被誤判成「不是 ETF」，導致 get_etf_list()、scan_priority 的 ETF 優先權完全抓不到它們。
def _is_valid_code(code: str) -> bool:
    """
    有效代碼格式：
    - 一般股票 / 舊制 ETF：4~6 碼純數字（如 2330、0050、006208）
    - 槓桿／反向／主動式 ETF：4~6 碼數字 + 1 碼英文字母（如 00631L、00632R、00400A）
    """
    if not code: return False
    if code.isdigit():
        return 4 <= len(code) <= 6
    if len(code) >= 2 and code[:-1].isdigit() and code[-1].isalpha():
        return 4 <= len(code) <= 7
    return False

def _is_etf_code(code: str) -> bool:
    """台股慣例：ETF／ETN／受益憑證代碼一律以「00」開頭，一般股票代碼不會用這個區間。"""
    return code.startswith("00")

def _fetch_twse_list() -> List[Dict]:
    try:
        r = requests.get(TWSE_LIST_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        stocks = []
        for item in r.json():
            code = item.get("Code","").strip()
            name = item.get("Name","").strip()
            if not code or not name: continue
            if not _is_valid_code(code): continue
            if code in BLACKLIST_CODES: continue
            if any(kw in name for kw in BLACKLIST_KEYWORDS): continue
            try:
                vol   = float(item.get("TradeVolume","0").replace(",","") or 0)
                close = float(item.get("ClosingPrice","0").replace(",","") or 0)
            except: continue
            stocks.append({
                "code": code, "ticker": f"{code}.TW", "name": name,
                "market": "TSE", "close": close,
                "volume_lots": round(vol/1000, 0),
                "sector": "", "is_etf": _is_etf_code(code),
                "size_cat": "", "scan_priority": 9,
            })
        logger.info(f"TWSE: {len(stocks)} 檔")
        return stocks
    except Exception as e:
        logger.error(f"_fetch_twse_list: {e}"); return []

def _fetch_tpex_list() -> List[Dict]:
    try:
        r = requests.get(TPEX_LIST_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        stocks = []
        for item in r.json():
            code = str(item.get("SecuritiesCompanyCode","")).strip()
            name = str(item.get("CompanyName","")).strip()
            # ★ 修正：跟 TWSE 那邊同一個問題——原本 `not code.isdigit()` 會把上櫃市場少數
            #   帶字母後綴的 ETF/受益證券代碼也濾掉；`is_etf` 原本更是直接寫死 False，
            #   代表就算上櫃真的掛牌 ETF，這裡也永遠不會被標成 ETF。改成跟 TWSE 共用同一套判斷。
            if not code or not name or not _is_valid_code(code): continue
            if code in BLACKLIST_CODES: continue
            try:
                close = float(str(item.get("Close","0")).replace(",","") or 0)
                vol   = float(str(item.get("TradingShares","0")).replace(",","") or 0)
            except: continue
            stocks.append({
                "code": code, "ticker": f"{code}.TWO", "name": name,
                "market": "OTC", "close": close,
                "volume_lots": round(vol/1000, 0),
                "sector": "", "is_etf": _is_etf_code(code),
                "size_cat": "", "scan_priority": 9,
            })
        logger.info(f"TPEX: {len(stocks)} 檔")
        return stocks
    except Exception as e:
        logger.error(f"_fetch_tpex_list: {e}"); return []

def _fetch_sector_info() -> Dict[str, str]:
    try:
        r = requests.get(TWSE_INFO_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200: return {}
        return {
            str(i.get("公司代號","")).strip(): str(i.get("產業類別","")).strip()
            for i in r.json()
        }
    except: return {}

def _classify_size(close, volume_lots) -> str:
    est = close * volume_lots * 250 / 1e8
    if est > 500: return "大型股"
    elif est > 100: return "中型股"
    return "小型股"

def build_universe(force_refresh=False) -> List[Dict]:
    """回傳 list of dict（取代原本的 DataFrame）"""
    os.makedirs("instance", exist_ok=True)
    if not force_refresh and _is_cache_valid():
        logger.info("使用快取品種清單")
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    logger.info("下載全市場品種清單...")
    # ★ 修正：先把當天的處置股/注意股代號抓回來、填進 BLACKLIST_CODES，
    #   _fetch_twse_list() 才過濾得掉——這個集合原本從沒被寫入過。
    global BLACKLIST_CODES
    fresh_blacklist = _fetch_disposal_and_attention_codes()
    if fresh_blacklist:
        BLACKLIST_CODES = fresh_blacklist
    else:
        logger.warning("處置/注意股票清單抓取失敗，本次沿用舊名單（可能為空）")
    tse  = _fetch_twse_list()
    tpex = _fetch_tpex_list()
    all_stocks = tse + tpex

    if not all_stocks:
        logger.error("無法下載品種清單，使用備份")
        return _get_fallback_universe()

    sector_map = _fetch_sector_info()

    result = []
    for s in all_stocks:
        if s["close"] <= 5: continue
        if s["volume_lots"] < THRESH["min_avg_volume"]: continue
        s["sector"] = sector_map.get(s["code"], "其他")
        s["size_cat"] = "ETF" if s["is_etf"] else _classify_size(s["close"], s["volume_lots"])
        s["scan_priority"] = 1 if s["is_etf"] else SECTOR_SCAN_PRIORITY.get(s["sector"], 9)
        result.append(s)

    # 排序：優先權 → 成交量
    result.sort(key=lambda x: (x["scan_priority"], -x["volume_lots"]))

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    logger.info(f"品種清單: {len(result)} 檔")
    return result

def get_scan_batches(batch_size=None) -> List[List[str]]:
    batch_size = batch_size or SYSTEM["scan_batch_size"]
    tickers = [s["ticker"] for s in build_universe()]
    return [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

def get_stock_info(ticker: str) -> Optional[Dict]:
    universe = build_universe()
    for s in universe:
        if s["ticker"] == ticker:
            return s
    return None

def get_sector_stocks(sector: str) -> List[str]:
    return [s["ticker"] for s in build_universe() if s.get("sector") == sector]

def get_etf_list() -> List[str]:
    return [s["ticker"] for s in build_universe() if s.get("is_etf")]

def get_tw50_components() -> List[str]:
    return [
        "2330.TW","2317.TW","2454.TW","2308.TW","2382.TW",
        "2412.TW","2303.TW","3711.TW","2881.TW","2882.TW",
        "2891.TW","2886.TW","2884.TW","2357.TW","2376.TW",
        "2379.TW","3008.TW","2002.TW","1301.TW","1303.TW",
        "2603.TW","2615.TW","2609.TW","6669.TW","3231.TW",
        "2377.TW","2353.TW","4904.TW","2408.TW","3034.TW",
        "2356.TW","2337.TW","1216.TW","1402.TW","2105.TW",
        "2207.TW","2327.TW","2474.TW","3045.TW","3037.TW",
        "4938.TW","5871.TW","6505.TW","8046.TW","9910.TW",
        "2885.TW","2883.TW","5880.TW","2887.TW","2890.TW",
    ]

def _get_fallback_universe() -> List[Dict]:
    from config import WATCHLIST_CORE
    result = []
    for ticker, info in WATCHLIST_CORE.items():
        code = ticker.replace(".TW","").replace(".TWO","")
        result.append({
            "ticker": ticker, "code": code, "name": info["name"],
            "market": "TSE", "sector": info.get("cat","其他"),
            "size_cat": "大型股", "close": 100.0, "volume_lots": 5000,
            "scan_priority": info.get("priority",3),
            "is_etf": info.get("cat")=="ETF",
        })
    return result

def refresh_universe_daily():
    universe = build_universe(force_refresh=True)
    logger.info(f"品種更新完成: {len(universe)} 檔")
    return len(universe)

# ── app.py 的 api_universe 路由需對應修改 ──
# 原本：df.groupby("sector").size().to_dict()
# 改成：
def get_sector_count() -> Dict[str, int]:
    counts = {}
    for s in build_universe():
        sec = s.get("sector","其他")
        counts[sec] = counts.get(sec, 0) + 1
    return counts
