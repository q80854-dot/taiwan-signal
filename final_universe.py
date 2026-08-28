"""
stock_universe.py — 台股全市場品種管理 v1.0
每日自動從 TWSE/TPEX 下載最新上市上櫃清單
"""
import os, time, logging, requests
import pandas as pd
from typing import List, Dict, Optional
from config import SIGNAL_THRESHOLDS as THRESH, SYSTEM

logger = logging.getLogger(__name__)

CACHE_PATH = "instance/stock_universe.csv"
CACHE_TTL  = 86400

TWSE_LIST_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_LIST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TWSE_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

BLACKLIST_KEYWORDS = ["全額交割", "處置股票", "注意股票", "下市", "停止買賣"]
BLACKLIST_CODES = set()

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

def _fetch_twse_list():
    try:
        r = requests.get(TWSE_LIST_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        stocks = []
        for item in r.json():
            code = item.get("Code","").strip()
            name = item.get("Name","").strip()
            if not code or not name: continue
            if len(code) not in (4,5) or not code.isdigit(): continue
            if code in BLACKLIST_CODES: continue
            if any(kw in name for kw in BLACKLIST_KEYWORDS): continue
            vol   = float(item.get("TradeVolume","0").replace(",","") or 0)
            close = float(item.get("ClosingPrice","0").replace(",","") or 0)
            stocks.append({
                "code": code, "ticker": f"{code}.TW", "name": name,
                "market": "TSE", "close": close,
                "volume_lots": round(vol/1000,0), "sector": "", "is_etf": len(code)==5,
            })
        logger.info(f"TWSE: {len(stocks)} 檔")
        return stocks
    except Exception as e:
        logger.error(f"_fetch_twse_list: {e}"); return []

def _fetch_tpex_list():
    try:
        r = requests.get(TPEX_LIST_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        stocks = []
        for item in r.json():
            code = str(item.get("SecuritiesCompanyCode","")).strip()
            name = str(item.get("CompanyName","")).strip()
            if not code or not name or not code.isdigit(): continue
            if code in BLACKLIST_CODES: continue
            try:
                close = float(str(item.get("Close","0")).replace(",","") or 0)
                vol   = float(str(item.get("TradingShares","0")).replace(",","") or 0)
            except: continue
            stocks.append({
                "code": code, "ticker": f"{code}.TWO", "name": name,
                "market": "OTC", "close": close,
                "volume_lots": round(vol/1000,0), "sector": "", "is_etf": False,
            })
        logger.info(f"TPEX: {len(stocks)} 檔")
        return stocks
    except Exception as e:
        logger.error(f"_fetch_tpex_list: {e}"); return []

def _fetch_sector_info():
    try:
        r = requests.get(TWSE_INFO_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200: return {}
        return {str(i.get("公司代號","")).strip(): str(i.get("產業類別","")).strip() for i in r.json()}
    except: return {}

def _classify_size(close, volume_lots):
    est = close * volume_lots * 250 / 1e8
    if est > 500: return "大型股"
    elif est > 100: return "中型股"
    return "小型股"

def build_universe(force_refresh=False):
    os.makedirs("instance", exist_ok=True)
    if not force_refresh and _is_cache_valid():
        logger.info("使用快取品種清單")
        return pd.read_csv(CACHE_PATH)
    tse  = _fetch_twse_list()
    tpex = _fetch_tpex_list()
    all_stocks = tse + tpex
    if not all_stocks:
        logger.error("無法下載品種清單，使用備份")
        return _get_fallback_universe()
    sector_map = _fetch_sector_info()
    df = pd.DataFrame(all_stocks)
    df["sector"]   = df["code"].map(sector_map).fillna("其他")
    df = df[df["close"] > 5]
    df = df[df["volume_lots"] >= THRESH["min_avg_volume"]]
    df["size_cat"] = df.apply(lambda r: "ETF" if r["is_etf"] else _classify_size(r["close"], r["volume_lots"]), axis=1)
    df["scan_priority"] = df["sector"].map(lambda s: SECTOR_SCAN_PRIORITY.get(s, 9))
    df.loc[df["is_etf"], "scan_priority"] = 1
    df = df.sort_values(["scan_priority","volume_lots"], ascending=[True,False]).reset_index(drop=True)
    df.to_csv(CACHE_PATH, index=False)
    logger.info(f"品種清單: {len(df)} 檔")
    return df

def get_scan_batches(batch_size=None):
    batch_size = batch_size or SYSTEM["scan_batch_size"]
    tickers = build_universe()["ticker"].tolist()
    return [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

def get_stock_info(ticker):
    df = build_universe()
    row = df[df["ticker"] == ticker]
    return row.iloc[0].to_dict() if not row.empty else None

def get_tw50_components():
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

def _get_fallback_universe():
    from config import WATCHLIST_CORE
    rows = []
    for ticker, info in WATCHLIST_CORE.items():
        code = ticker.replace(".TW","").replace(".TWO","")
        rows.append({
            "ticker": ticker, "code": code, "name": info["name"],
            "market": "TSE", "sector": info.get("cat","其他"),
            "size_cat": "大型股", "close": 100.0, "volume_lots": 5000,
            "scan_priority": info.get("priority",3), "is_etf": info.get("cat")=="ETF",
        })
    return pd.DataFrame(rows)

def refresh_universe_daily():
    df = build_universe(force_refresh=True)
    logger.info(f"品種更新完成: {len(df)} 檔")
    return len(df)
