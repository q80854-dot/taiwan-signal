"""
stock_universe.py — 台股全市場品種管理 v1.1（無 pandas 版）
移除 pandas 依賴，改用純 Python list/dict
"""
import os, time, logging, requests, json
from typing import List, Dict, Optional
from config import SIGNAL_THRESHOLDS as THRESH, SYSTEM

logger = logging.getLogger(__name__)

# ★ 修正：2026-09-03——www.tpex.org.tw 的伺服器憑證缺少標準 X.509
# 「Subject Key Identifier」欄位，較新版 OpenSSL/urllib3 在憑證鏈驗證時會直接
# 判定失敗（SSLCertVerificationError: Missing Subject Key Identifier），這跟
# 連線本身、跟資料是否正確完全無關，是 TPEx 官網憑證設定本身的已知瑕疵
# （data_fetcher.py 抓 TPEx 指數時，同一個網域也遇過一連串 SSL/憑證/
# Cloudflare 問題，最後同樣是放寬憑證驗證處理，見該檔 _fetch_tpex() 的說明）。
# 這裡只在遇到這個特定 SSL 錯誤時才退回不驗證憑證重試一次；這支 API 只讀取
# 公開的上櫃股票報價清單，不是登入、不是交易，沒有機敏資料會經過這條連線，
# 關閉憑證驗證的風險可接受——總比每天全市場掃描直接漏掉全部上櫃股票好
# （原本例外會被外層 try/except 吃掉、回傳空清單，代表當天所有上櫃股票
# 都不會被掃描，且完全沒有清楚提示是「這個原因」造成的，只在 log 留一行
# error）。
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CACHE_TTL  = 86400
# ★ 新增：2026-09-29——使用者要求個股研究頁要能查全部上市櫃股票，不能只限
# build_universe() 套了 200張成交量門檻之後的掃描池候選。這支「未過濾原始
# 清單」快取是 build_universe()（掃描用，套門檻）跟 build_full_universe()
# （研究查詢用，不套門檻）共用的底層資料來源，同一次刷新只打一次 TWSE/TPEX
# API，不會因為多了研究頁功能就讓每日 API 呼叫量翻倍。
RAW_CACHE_PATH = "instance/stock_universe_raw.json"

TWSE_LIST_URL   = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_LIST_URL   = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
TWSE_INFO_URL   = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_INFO_URL   = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"  # 上櫃公司基本資料（含產業別代碼）
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

# ★ 新增：2026-09-16——處置/注意股黑名單原本只有在「整份全市場清單」被完整重新
# 下載時才會刷新（24小時 CACHE_TTL，或 refresh_universe_daily() 的每日 16:00
# force_refresh），代表使用者若在非排程更新時間點手動觸發掃描（例如盤中、隔天
# 一早才想到要補掃），用的可能是前一天下午更新的舊黑名單——期間如果有新股票被
# 公告處置/注意，掃描完全不會排除它，這是安全相關的問題（處置股波動極端、
# 不該被當成一般標的推薦進出場）。這裡讓黑名單有自己獨立的、短很多的新鮮度
# （15分鐘），不再綁死在 24小時的全市場清單快取上；但也不能每次 build_universe()
# 被呼叫就打一次 API——get_stock_info() 在一次全市場掃描裡會被呼叫上千次，
# 這裡额外加一個時間戳門檻，實際的 HTTP 請求最多每 15 分鐘才真正發生一次。
_BLACKLIST_TTL = 900
_blacklist_refreshed_at = 0.0

def _refresh_blacklist_if_stale():
    global BLACKLIST_CODES, _blacklist_refreshed_at
    if time.time() - _blacklist_refreshed_at < _BLACKLIST_TTL:
        return
    fresh = _fetch_disposal_and_attention_codes()
    if fresh:
        BLACKLIST_CODES = fresh
        _blacklist_refreshed_at = time.time()
    else:
        # 刷新失敗時沿用舊名單（總比完全沒有黑名單好），但仍然更新時間戳，
        # 避免短時間內因為上游 API 持續失敗而每次呼叫都重打一次
        _blacklist_refreshed_at = time.time()
        if BLACKLIST_CODES:
            logger.warning("_refresh_blacklist_if_stale: 處置/注意股清單刷新失敗，沿用舊名單")
        else:
            logger.warning("_refresh_blacklist_if_stale: 處置/注意股清單刷新失敗且無舊名單可沿用")

def _filter_blacklist(universe: List[Dict]) -> List[Dict]:
    if not BLACKLIST_CODES:
        return universe
    filtered = [s for s in universe if s.get("code") not in BLACKLIST_CODES]
    removed = len(universe) - len(filtered)
    if removed:
        logger.info(f"_filter_blacklist: 依最新處置/注意名單即時排除 {removed} 檔（不受全市場清單24小時快取影響）")
    return filtered

def _fetch_disposal_and_attention_codes() -> set:
    """
    抓 TWSE 官方「公布處置股票」(punish) + 「當日公布注意股票」(notice) 的即時名單，
    回傳證券代號集合。這是台股全市場都掃時，排除波動最極端族群的關鍵一步。
    上櫃(TPEX)的處置／注意股由 market_extras 從 TPEx OpenAPI（tpex_disposal_information、
    tpex_trading_warning_information）取得，併入同一份排除名單（2026-10-07 起）。
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
    # 暫停交易中、分盤／變更交易的標的買不到或流動性極差，一併排除（market_extras；失敗不影響原清單）
    try:
        import market_extras
        _hx = market_extras.hard_exclude_codes()
        if _hx:
            logger.info(f"暫停交易／變更交易排除：{len(_hx)} 檔")
            codes |= _hx
        _ow = market_extras.otc_watch_codes()
        if _ow:
            logger.info(f"上櫃處置／注意股排除：{len(_ow)} 檔")
            codes |= _ow
    except Exception as e:
        logger.warning(f"market_extras 排除清單失敗：{e}")
    return codes

# ★ 修正：2026-09-29——這裡原本的 key（"半導體"、"電子零組件"…不帶「業」/
# 「工業」字尾）是對著「產業分類完全沒作用」那個 bug 修好之前、sector 欄位
# 永遠是空字串／預設值「其他」的世界寫的，從來沒有機會被真正比對到。現在
# _fetch_sector_info() 已經改成回傳 TWSE 官方標準分類名稱（見上面
# INDUSTRY_CODE_MAP 的說明，名稱帶不帶「業」/「工業」字尾是照官方原文），
# 這裡的 key 要跟著改成完全一致的官方名稱，不然 SECTOR_SCAN_PRIORITY.get(
# s["sector"], 9) 永遠比對不到、全部靜默落回預設優先權 9——這正是使用者
# 回報「掃描優先順序實際上一直都沒有生效」的同一個根因。
# "AI概念"／"電力設備" 這兩個字串移除：它們不是 TWSE 官方產業分類，只存在
# 於 config.py 的 WATCHLIST_CORE（人工挑選的核心觀察清單，跟這裡的全市場
# 掃描池是兩條獨立資料路徑），sig["sector"] 不會出現這兩個值，留著也是死碼。
SECTOR_SCAN_PRIORITY = {
    "半導體業": 1, "電腦及週邊設備業": 2,
    "電子零組件業": 2, "電機機械": 2,
    "光電業": 3, "通信網路業": 3, "資訊服務業": 3,
    "其他電子業": 3, "數位雲端業": 3,
    "金融保險": 4, "生技醫療業": 4, "航運業": 4,
    "ETF": 1, "鋼鐵工業": 5, "化學工業": 5, "塑膠工業": 5,
    "建材營造": 6, "食品工業": 6, "紡織纖維": 6, "其他": 9,
}

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

# ★ 新增：2026-09-29——使用者要求「市場總覽頁」要有漲跌家數/產業排行，這些
# 都需要每檔股票的當日漲跌，STOCK_DAY_ALL / tpex_mainboard_quotes 這兩個
# OpenAPI 理論上都會附一個「Change」欄位（漲跌價差），但這支程式碼目前跑在
# 沙盒容器裡對外連線被 proxy 擋掉，沒辦法在寫程式的當下直接打 API 驗證欄位
# 名稱/格式是否跟文件一致。這裡刻意寫成「解析失敗就整檔記 None，不是猜一個
# 假數字」，上線後會再用正式站實測結果回頭確認——如果欄位名稱猜錯，None
# 會讓對應股票被市場總覽頁的漲跌家數統計跳過（不計入漲/跌/平），不會顯示
# 錯誤的漲跌方向。
def _parse_change(raw) -> Optional[float]:
    try:
        s = str(raw).strip().replace(",", "")
        if not s or s in ("--", "-", "X0.00", "X0"): return None
        return float(s)
    except Exception:
        return None

# ★ 新增：2026-09-29——使用者回報「成交量的張數全部都有問題」。實測驗證
# （用 WebFetch 直接打 TWSE 官方 STOCK_DAY 單股歷史 API 現場核對 2330、00400A
# 兩檔）：vol/1000 這條換算公式本身沒有錯，算出來的張數跟 TWSE 官方公佈的
# 數字逐位元相符。真正的落差在於：TWSE STOCK_DAY_ALL 這支「全市場快照」API
# 本身只會在「每個交易日收盤後」才更新當天的資料，如果使用者是在收盤前，
# 或是 TWSE 官方資料本身還沒更新完成的時間點查詢，API 回傳的會是「上一個
# 已收盤交易日」的舊資料，而這支程式原本完全沒有把「這份快照實際上是哪一
# 個交易日的資料」記錄下來或顯示給使用者看——使用者看到的只有我們自己的
# 抓取時間戳（fetched_at），會被誤會成「今天的即時資料」，但實際上可能是
# 前幾個交易日的舊數字，讓人誤以為是張數「算錯了」。這裡把 TWSE 官方回傳
# 的 Date 欄位（民國年格式，如 "1150924" = 民國115年09月24日）解析出來，
# 存到每一筆股票資料的 quote_date 欄位，讓 _get_raw_universe() 可以往上
# attach 一個全市場統一的「這份資料實際上是哪一天收盤後的資料」欄位，
# 前端會用這個欄位明確標示，而不是只顯示我們自己的抓取時間。
def _parse_roc_date(raw) -> Optional[str]:
    """民國年日期字串轉西元 ISO 格式。支援 TWSE 常見的兩種格式：
    純數字 7 碼「1150924」(YYYMMDD) 或帶斜線「115/09/24」。
    解析失敗回傳 None（不猜測，避免顯示錯誤的日期誤導使用者）。"""
    try:
        s = str(raw).strip()
        if not s: return None
        if "/" in s:
            parts = s.split("/")
            if len(parts) != 3: return None
            roc_y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            if not s.isdigit() or len(s) not in (6, 7): return None
            roc_y = int(s[:-4]); m = int(s[-4:-2]); d = int(s[-2:])
        if not (1 <= m <= 12 and 1 <= d <= 31): return None
        year = roc_y + 1911
        return f"{year:04d}-{m:02d}-{d:02d}"
    except Exception:
        return None

def _pf(x) -> Optional[float]:
    """官方欄位轉浮點數；空字串、"--"、0 以下一律視為沒有。"""
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v > 0 else None
    except Exception:
        return None


def _fetch_twse_list() -> List[Dict]:
    try:
        r = requests.get(TWSE_LIST_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_twse_list: HTTP {r.status_code} - {r.text[:200]}")
            return []
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
            chg = _parse_change(item.get("Change"))
            chg_pct = round(chg/(close-chg)*100, 2) if (chg is not None and close-chg > 0) else None
            stocks.append({
                "code": code, "ticker": f"{code}.TW", "name": name,
                "market": "TSE", "close": close,
                "open": _pf(item.get("OpeningPrice")), "high": _pf(item.get("HighestPrice")), "low": _pf(item.get("LowestPrice")),
                "volume_lots": round(vol/1000, 0),
                "change": chg, "change_pct": chg_pct,
                "sector": "", "is_etf": _is_etf_code(code),
                "size_cat": "", "scan_priority": 9,
                "quote_date": _parse_roc_date(item.get("Date")),
            })
        logger.info(f"TWSE: {len(stocks)} 檔")
        return stocks
    except Exception as e:
        logger.error(f"_fetch_twse_list: {e}"); return []

def _fetch_tpex_list() -> List[Dict]:
    try:
        try:
            r = requests.get(TPEX_LIST_URL, headers=HEADERS, timeout=15)
        except requests.exceptions.SSLError as e:
            logger.warning(f"_fetch_tpex_list: SSL 憑證驗證失敗（{e}），改用不驗證憑證重試一次")
            r = requests.get(TPEX_LIST_URL, headers=HEADERS, timeout=15, verify=False)
        if r.status_code != 200:
            logger.warning(f"_fetch_tpex_list: HTTP {r.status_code} - {r.text[:200]}")
            return []
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
            chg = _parse_change(item.get("Change"))
            chg_pct = round(chg/(close-chg)*100, 2) if (chg is not None and close-chg > 0) else None
            # ★ 新增：2026-09-29——TPEX 這支 API 目前無法從沙盒環境現場驗證欄位
            # 名稱（WebFetch 打這個網域一直被擋 403），所以這裡沒有偷懶假設
            # 一定是哪個 key，而是依序嘗試官方 OpenAPI 文件/其他上櫃端點常見的
            # 幾種可能命名，找到第一個能解析出合法日期的就用，全部失敗就是
            # None（前端會照實顯示「上櫃資料日期未知，以下市資料日期為準」，
            # 不會顯示一個猜錯的日期）。
            tpex_date = None
            for key in ("Date", "date", "TradingDate", "SecuritiesTradingDate", "d"):
                if key in item:
                    tpex_date = _parse_roc_date(item.get(key))
                    if tpex_date: break
            stocks.append({
                "code": code, "ticker": f"{code}.TWO", "name": name,
                "market": "OTC", "close": close,
                "open": _pf(item.get("Open")), "high": _pf(item.get("High")), "low": _pf(item.get("Low")),
                "volume_lots": round(vol/1000, 0),
                "change": chg, "change_pct": chg_pct,
                "sector": "", "is_etf": _is_etf_code(code),
                "size_cat": "", "scan_priority": 9,
                "quote_date": tpex_date,
            })
        logger.info(f"TPEX: {len(stocks)} 檔")
        return stocks
    except Exception as e:
        logger.error(f"_fetch_tpex_list: {e}"); return []

# ★ 修正：2026-09-29——使用者回報「產業分類完全沒作用」，實測用 WebFetch 直接
# 打 TWSE OpenAPI t187ap03_L 現場核對：這支 API 回傳的欄位其實叫「產業別」
# （不是原本程式在讀的「產業類別」——這兩個字串長得很像，但完全是不同 key，
# 用 .get() 讀不存在的 key 不會噴錯，只會安靜拿到空字串，這是原本這個 bug
# 完全沒被任何錯誤訊息暴露出來的原因），而且「產業別」裡放的是兩碼數字代碼
# （例如 "24"），不是中文名稱（例如「半導體業」）——這代表就算欄位名稱修對了，
# 不加代碼對照表，sector 欄位存的還是一個沒人看得懂的數字字串，前端顯示、
# SECTOR_SCAN_PRIORITY 掃描優先權、scanner.py 的同產業相關性上限，這些比對
# 中文名稱的地方全部都會比對失敗。
#
# 下面這份代碼表是 TWSE 官方「上市公司產業類別劃分暨調整要點」的標準 32 類
# 分類（另外 80/91/97/99 是管理股票/存託憑證/ETF/未分類這幾個特殊類別）。
# 已用 WebFetch 現場交叉比對驗證過至少 20 組代碼／公司（例如 1101 台泥→01、
# 1301 台塑→03、1402 遠東新→04、2002 中鋼→10、2603 長榮→15、2891 中信金→17、
# 2330 台積電／2454 聯發科／2379 瑞昱→24、4938 和碩／3231 緯創→25、
# 3008 大立光→26、2412 中華電→27、2308 台達電／2382 廣達→28、2317 鴻海→31，
# 皆與官方英文版 ISIN 清單顯示的分類名稱一致），但這份表本身是依公開文件／
# 多個資料點交叉核對重建，不是從單一權威來源整份下載，仍有極小機率個別
# 代碼（尤其是使用頻率極低的舊代碼，如 06/12/13/16/18/32/33/34）跟官方最新
# 定義有出入——如果上線後發現特定股票的產業分類明顯不合理，請直接回報代號，
# 那會是這份表要修正的地方，不是欄位讀取邏輯本身的問題。
INDUSTRY_CODE_MAP = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造", "15": "航運業", "16": "觀光事業", "17": "金融保險",
    "18": "貿易百貨", "20": "其他", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業",
    "32": "文化創意業", "33": "農業科技業", "34": "電子商務業",
    "35": "綠能環保業", "36": "數位雲端業", "37": "運動休閒業",
    "38": "居家生活業", "80": "管理股票", "91": "存託憑證", "97": "ETF",
    "99": "未分類",
}

def _fetch_sector_info() -> Dict[str, str]:
    # ★ 修正：2026-08-30——這個函式原本非 200、或任何例外，一律安靜回傳 {}，
    #   完全沒有 log。這個函式的結果會被 build_universe() 拿去給每一檔股票標
    #   sector，如果失敗、每一檔股票的 sector 都會是空字串——這件事本身在
    #   scanner.py 的同產業去重邏輯下有嚴重的下游影響（見 scanner.py 的修正
    #   說明：所有股票落在同一個 sector，會讓當天訊號被去重到只剩 1 檔），
    #   所以這裡的失敗一定要留下 log，不能是黑盒子。
    try:
        r = requests.get(TWSE_INFO_URL, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"_fetch_sector_info: HTTP {r.status_code} - {r.text[:200]}")
            return {}
        result = {}
        unmapped_codes = set()
        for i in r.json():
            code = str(i.get("公司代號","")).strip()
            # ★ 修正：2026-09-29——原本讀的是「產業類別」（不存在的欄位名，
            # 永遠拿到空字串），改讀 TWSE 官方實際欄位「產業別」，值是兩碼
            # 數字代碼，要再查表轉成中文名稱。查不到的代碼記下來一次性 log，
            # 不要讓某個代碼查表失敗又是一次「安靜變空字串」。
            raw_industry_code = str(i.get("產業別","")).strip()
            if not code or not raw_industry_code: continue
            name = INDUSTRY_CODE_MAP.get(raw_industry_code)
            if name is None:
                unmapped_codes.add(raw_industry_code)
                continue
            result[code] = name
        if unmapped_codes:
            logger.warning(f"_fetch_sector_info: 有 {len(unmapped_codes)} 個產業代碼不在對照表裡（INDUSTRY_CODE_MAP 需要補上）: {sorted(unmapped_codes)}")
        if not result:
            logger.warning("_fetch_sector_info: HTTP 200 但沒有解析出任何一筆產業分類資料")
        # 2026-10-06：補上櫃產業。原本上櫃一律「其他」（1300 多檔），讓產業排行失真。
        # 櫃買中心「上櫃公司基本資料」同樣有兩碼產業代碼；欄位名稱用關鍵字比對，
        # 抓不到就留下實際欄位名的 log，不讓它安靜失敗。
        try:
            r2 = requests.get(TPEX_INFO_URL, headers=HEADERS, timeout=20)
            if r2.status_code == 200:
                rows = r2.json()
                n0 = len(result)
                if rows:
                    keys = list(rows[0].keys())
                    kc = next((k for k in keys if "CompanyCode" in k or "公司代號" in k or k.lower().endswith("code") and "Industry" not in k), None)
                    ki = next((k for k in keys if "Industry" in k or "產業" in k), None)
                    if not kc or not ki:
                        logger.warning(f"_fetch_sector_info(TPEx): 找不到代號/產業欄位，實際欄位={keys}")
                    else:
                        miss = set()
                        for i in rows:
                            code = str(i.get(kc, "")).strip()
                            ic = str(i.get(ki, "")).strip()
                            if not code or not ic or code in result: continue
                            ic = ic.zfill(2) if ic.isdigit() else ic
                            nm = INDUSTRY_CODE_MAP.get(ic)
                            if nm is None:
                                miss.add(ic); continue
                            result[code] = nm
                        logger.info(f"_fetch_sector_info: 上櫃補產業 {len(result)-n0} 檔（欄位 {kc}/{ki}）")
                        if miss:
                            logger.warning(f"_fetch_sector_info(TPEx): 未對照的產業代碼 {sorted(miss)}")
            else:
                logger.warning(f"_fetch_sector_info(TPEx): HTTP {r2.status_code}")
        except Exception as e:
            logger.warning(f"_fetch_sector_info(TPEx): {e}")
        return result
    except Exception as e:
        logger.warning(f"_fetch_sector_info: {e}")
        return {}

# ★ 修正：2026-09-19——稽核發現這裡漏乘 1000：volume_lots 是「張」（1張=1000股，
# 見上面 build_universe 組資料時 "volume_lots": round(vol/1000, 0)，vol 本身已經是
# 「股數」），但這裡直接拿 volume_lots（張數）乘 close 去估「年化成交金額」，等於
# 少算了 1000 倍的股數換算。實際影響：像台積電這種大型股，估出來的 est 會被低估
# 1000倍，導致幾乎所有股票都被誤判成「小型股」，而 size_cat 又會餵進
# risk_manager.py 的 SWING_PARAMS 去決定停損/停利/移動停損的參數，等於全市場的
# 風控參數長期套錯一組（本該用大型股的寬鬆停損，卻套用小型股的參數）。這裡補回
# 缺的 ×1000（張→股）。
def _classify_size(close, volume_lots) -> str:
    est = close * volume_lots * 1000 * 250 / 1e8
    if est > 500: return "大型股"
    elif est > 100: return "中型股"
    return "小型股"

# ★ 修正：2026-09-03——重大效能/記憶體 bug：get_stock_info() 每一檔股票都會呼叫
# 一次 build_universe()，全市場單次掃描 1000+ 檔，代表這個函式一次掃描會被呼叫
# 1000+ 次；先前每次呼叫都重新開檔、讀取、json.load() 整份全市場清單（上千筆
# dict），再線性掃過去找一檔——不只是浪費 CPU/硬碟 IO，更是每次都在 Python heap
# 上重新配置一份完整清單，用完馬上變垃圾等下次 GC，30 分鐘、1000+ 次的反覆配置/
# 回收，很可能是 Render 512MB 方案上 instance 記憶體壓力、掃描中途被平台重啟的
# 另一個（甚至更主要的）原因，跟 data_fetcher.py 那個無上限快取是同一類問題。
# 這裡加一層簡單的行程內記憶體快取：只要硬碟快取沒過期，同一個 process 內直接
# 回傳記憶體裡那份，不用每次都重新讀檔案、重新反序列化。
_raw_mem_cache: Optional[Dict] = None
_raw_mem_cache_at: float = 0.0

def _save_raw_to_db(payload):
    """★ 2026-10-06：Render 的硬碟每次部署都會清空，原本重啟後第一位訪客得等 8～20 秒重抓全市場。
    把清單同步存進資料庫，重啟時直接載入。"""
    try:
        from state_store import store
        store.set_meta("universe_raw", payload)
    except Exception as e:
        logger.warning(f"_save_raw_to_db: {e}")


def _load_raw_from_db():
    try:
        from state_store import store
        p = store.get_meta("universe_raw")
        if isinstance(p, dict) and p.get("stocks"):
            return p
    except Exception as e:
        logger.warning(f"_load_raw_from_db: {e}")
    return None


def _expected_latest_date(now=None) -> str:
    """推估官方日行情「現在應該至少是哪一天」：平日 15:30 之後＝今天；其餘時間＝前一個平日。
    （不判斷國定假日；假日時官方不會更新，只會多抓幾次，成本很低。）"""
    import pytz
    from datetime import datetime, timedelta
    now = now or datetime.now(pytz.timezone("Asia/Taipei"))
    d = now.date()
    if d.weekday() <= 4 and now.hour * 60 + now.minute >= 15 * 60 + 30:
        return d.isoformat()
    d -= timedelta(days=1)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d.isoformat()


def _date_stale(raw, now=None) -> bool:
    """快取裡的官方行情日期比「應有的最新交易日」舊，且距上次抓取超過 30 分鐘 → 視為過期，要重抓。
    （2026-10-07：原本只看抓取時間 24 小時，16:00 抓到證交所還沒更新的前一天資料後，會一路沿用到隔天。）"""
    try:
        ds = [x for x in (raw.get("tse_quote_date"), raw.get("otc_quote_date")) if x]
        if not ds:
            return False
        if min(ds) >= _expected_latest_date(now):
            return False
        return time.time() - raw.get("fetched_at", 0) > 1800
    except Exception:
        return False


def refresh_if_date_stale() -> bool:
    """排程用：官方日行情日期過期就強制重抓。回傳是否有重抓。"""
    raw = _raw_mem_cache or _load_raw_from_db()
    if raw is None or _date_stale(raw):
        build_universe(force_refresh=True)
        return True
    return False


def _raw_cache_valid():
    if not os.path.exists(RAW_CACHE_PATH): return False
    return time.time() - os.path.getmtime(RAW_CACHE_PATH) < CACHE_TTL

def _get_raw_universe(force_refresh=False) -> Dict:
    """抓（或讀快取）全市場「未經任何流動性/價格門檻過濾」的原始清單＋產業分類。
    回傳 {"stocks":[...每檔股票原始資料...], "sector_map":{代號:產業}, "fetched_at":
    抓取時間戳, "fetch_ok": 本次是否真的成功抓到資料（False代表用的是舊快取或備援）}。
    build_universe()（掃描池，套 200張/5元門檻）跟 build_full_universe()（個股
    研究頁查詢用，不套門檻，讓使用者查得到全部上市櫃股票）共用這份底層資料，
    同一次刷新只打一次 TWSE/TPEX 官方 API。
    """
    global _raw_mem_cache, _raw_mem_cache_at
    os.makedirs("instance", exist_ok=True)

    if not force_refresh and not os.path.exists(RAW_CACHE_PATH):
        _p = _load_raw_from_db()
        if _p and time.time() - _p.get("fetched_at", 0) < CACHE_TTL and not _date_stale(_p):
            try:
                with open(RAW_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(_p, f, ensure_ascii=False)
            except Exception:
                pass
            _raw_mem_cache, _raw_mem_cache_at = _p, time.time()
            logger.info(f"品種清單：從資料庫載入（{len(_p['stocks'])} 檔），免重新下載")
            return _p

    if not force_refresh and _raw_cache_valid():
        if _raw_mem_cache is not None and time.time() - _raw_mem_cache_at < CACHE_TTL:
            _c = _raw_mem_cache
        else:
            with open(RAW_CACHE_PATH, "r", encoding="utf-8") as f:
                _c = json.load(f)
        if not _date_stale(_c):
            if _c is not _raw_mem_cache:
                logger.info("使用快取原始品種清單")
                _raw_mem_cache, _raw_mem_cache_at = _c, time.time()
            return _c
        logger.info("官方日行情的資料日期已過期，重新下載")

    logger.info("下載全市場原始品種清單（含上市+上櫃，未過濾）...")
    tse  = _fetch_twse_list()
    tpex = _fetch_tpex_list()
    all_stocks = tse + tpex

    if not all_stocks:
        logger.error("_get_raw_universe: 無法下載品種清單")
        if _raw_mem_cache is not None:
            logger.warning("_get_raw_universe: 本次刷新失敗，沿用記憶體裡的舊快取")
            return _raw_mem_cache
        if os.path.exists(RAW_CACHE_PATH):
            logger.warning("_get_raw_universe: 本次刷新失敗，改讀硬碟舊快取（可能已過期）")
            with open(RAW_CACHE_PATH, "r", encoding="utf-8") as f:
                _raw_mem_cache = json.load(f)
            _raw_mem_cache_at = time.time()
            return _raw_mem_cache
        return {"stocks": [], "sector_map": {}, "fetched_at": time.time(), "fetch_ok": False,
                "quote_trading_date": None, "tse_quote_date": None, "otc_quote_date": None,
                "dates_mismatch": False}

    sector_map = _fetch_sector_info()
    if not sector_map:
        logger.warning(
            f"_get_raw_universe: 產業分類資料抓取失敗，本次 {len(all_stocks)} 檔股票全部會被標成"
            f"「其他」——這會讓 scanner.py 的同產業去重邏輯把每天的訊號都收斂到只剩 1 檔"
            f"（已在 scanner.py 加防呆略過去重，但這裡還是先留下明確的根因記錄）"
        )

    # ★ 修正：2026-09-29——使用者回報「市場總覽的上漲/下跌家數全部加起來的
    # 數字其實都有錯」，實測驗證找到真正的根因：TWSE STOCK_DAY_ALL（上市）
    # 跟 TPEX tpex_mainboard_quotes（上櫃）這兩支官方 API「不是同時更新的」
    # ——現場核對發現 TWSE 這支目前還停在 2026-09-24（受 09-25~09-28 連續
    # 假期影響，官方尚未補上最新收盤資料），但 TPEX 那支已經是 2026-09-29
    # （今天）的資料。原本這裡只取一個「quote_trading_date」代表全市場
    # （優先用 TWSE 的日期），把上市/上櫃兩邊「實際上不是同一個交易日」的
    # 資料當成同一天，直接加總算「上漲家數/下跌家數」，等於把 09-24 那天
    # 上市股票的漲跌，跟 09-29 今天上櫃股票的漲跌混在一起相加——這個總數在
    # 兩邊日期不一致時本來就沒有意義，是使用者說「數字都有錯」的真正原因，
    # 不是加總的程式邏輯本身寫錯。修正：分別記錄 tse_quote_date／
    # otc_quote_date 兩個日期，讓 get_market_breadth()／前端可以各自算、
    # 各自標示，並在兩邊日期不同時明確示警，而不是假裝這是同一天的市場
    # 總覽。quote_trading_date 保留（優先 TWSE、退而求其次 TPEX）供舊欄位
    # 相容使用，但新增的兩個欄位才是正確判斷「這筆資料到底是哪一天」的依據。
    tse_quote_date = None
    for s in tse:
        if s.get("quote_date"):
            tse_quote_date = s["quote_date"]; break
    otc_quote_date = None
    for s in tpex:
        if s.get("quote_date"):
            otc_quote_date = s["quote_date"]; break
    quote_trading_date = tse_quote_date or otc_quote_date

    payload = {"stocks": all_stocks, "sector_map": sector_map,
               "fetched_at": time.time(), "fetch_ok": True,
               "quote_trading_date": quote_trading_date,
               "tse_quote_date": tse_quote_date,
               "otc_quote_date": otc_quote_date,
               "dates_mismatch": bool(tse_quote_date and otc_quote_date and tse_quote_date != otc_quote_date)}
    with open(RAW_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    _save_raw_to_db(payload)
    _raw_mem_cache, _raw_mem_cache_at = payload, time.time()
    logger.info(f"原始品種清單: {len(all_stocks)} 檔（上市+上櫃，未過濾）")
    return payload

def build_universe(force_refresh=False) -> List[Dict]:
    """回傳「掃描池」清單（list of dict）：套用 config.py THRESH['min_avg_volume']
    （目前 200 張）流動性門檻＋收盤價>5 元門檻，這是 scanner.py 每日掃描候選、
    市場總覽頁漲跌家數/產業排行/篩選器的資料來源——這個門檻是為了「值不值得
    產生交易訊號」設計的，避免對幾千檔冷門雞蛋水餃股跑技術指標（成本太高）。
    ★ 個股研究頁查詢單一股票不透過這個函式，改用不設門檻的 build_full_universe()
    （使用者明確要求：查詢範圍要涵蓋全部上市櫃股票，不能只限這裡的掃描池候選）。
    """
    _refresh_blacklist_if_stale()
    raw = _get_raw_universe(force_refresh)
    all_stocks, sector_map = raw["stocks"], raw["sector_map"]

    if not all_stocks:
        logger.error("build_universe: 無法取得原始品種清單，使用備份")
        return _get_fallback_universe()

    result = []
    for orig in all_stocks:
        s = dict(orig)  # 複製一份，不要動到 _get_raw_universe 快取裡的原始 dict
        if s["close"] <= 5: continue
        if s["volume_lots"] < THRESH["min_avg_volume"]: continue
        s["sector"] = sector_map.get(s["code"], "其他")
        s["size_cat"] = "ETF" if s["is_etf"] else _classify_size(s["close"], s["volume_lots"])
        s["scan_priority"] = 1 if s["is_etf"] else SECTOR_SCAN_PRIORITY.get(s["sector"], 9)
        result.append(s)

    # 排序：優先權 → 成交量
    result.sort(key=lambda x: (x["scan_priority"], -x["volume_lots"]))
    return _filter_blacklist(result)

# ★ 新增：2026-09-29——個股研究頁專用：回傳全部上市＋上櫃股票（只排除收盤價
# <=0 的無效資料列），不套用 build_universe() 的成交量/價格門檻，也不排除
# 處置/注意股（研究頁本來就該查得到，但用 is_disposal_or_attention 明確標出
# 來，讓使用者知道這檔目前被列管、波動可能極端）。額外附上 in_scan_universe
# 欄位，讓前端可以誠實告知使用者「這檔目前是否會被每日訊號掃描納入候選」。
def build_full_universe(force_refresh=False) -> List[Dict]:
    _refresh_blacklist_if_stale()
    raw = _get_raw_universe(force_refresh)
    all_stocks, sector_map = raw["stocks"], raw["sector_map"]
    if not all_stocks:
        logger.error("build_full_universe: 無法取得原始品種清單，使用備份")
        return _get_fallback_universe()
    result = []
    for orig in all_stocks:
        if orig["close"] <= 0: continue
        s = dict(orig)
        s["sector"] = sector_map.get(s["code"], "其他")
        s["size_cat"] = "ETF" if s["is_etf"] else _classify_size(s["close"], s["volume_lots"])
        s["in_scan_universe"] = s["close"] > 5 and s["volume_lots"] >= THRESH["min_avg_volume"]
        s["is_disposal_or_attention"] = s["code"] in BLACKLIST_CODES
        result.append(s)
    result.sort(key=lambda x: x["code"])
    _persist_official_volumes(result)
    return result


_live_cache = {"ts": 0.0, "snap": None}
_LIVE_MAX_AGE = 600          # 快照超過 10 分鐘就不採用（寧可顯示官方收盤資料，也不顯示過期的「即時」）


def snap_is_final(snap, now_ts) -> bool:
    """收盤後（台北時間 13:33 起）拍下、且與現在同一天的快照＝當日最終資料，收盤後仍可採用，不受 10 分鐘限制。"""
    try:
        from datetime import datetime, timedelta
        t = datetime.utcfromtimestamp(snap["at"]) + timedelta(hours=8)
        n = datetime.utcfromtimestamp(now_ts) + timedelta(hours=8)
        return t.date() == n.date() and t.hour * 60 + t.minute >= 13 * 60 + 33
    except Exception:
        return False


def _get_live_snapshot():
    """最近一次盤中快照（記憶體快取 20 秒）；過期或不是今天的回 None。"""
    import time as _t
    now = _t.time()
    if now - _live_cache["ts"] > 20:
        try:
            from state_store import store
            _live_cache["snap"] = store.get_meta("intraday_snapshot") or None
        except Exception:
            _live_cache["snap"] = None
        _live_cache["ts"] = now
    snap = _live_cache["snap"]
    if not snap or not snap.get("at"):
        return None
    if now - snap["at"] > _LIVE_MAX_AGE and not snap_is_final(snap, now):
        return None
    return snap


def live_status() -> Dict:
    """盤中即時覆蓋是否啟用，供前端標示。"""
    import time as _t
    snap = _get_live_snapshot()
    if not snap:
        return {"active": False}
    return {"active": True, "snapshot_at": snap["at"], "age_secs": round(_t.time() - snap["at"]),
            "final": snap_is_final(snap, _t.time()), "n_ok": snap.get("n_ok"), "completeness": snap.get("completeness"), "source": "證交所 MIS（官方盤中即時）"}


def build_live_universe(force_refresh=False) -> List[Dict]:
    """全市場清單；盤中若有新鮮的官方即時快照，就把價格／漲跌幅／成交量換成即時值（官方日行情日期早於今天才覆蓋）。
    今天沒成交的股票不沿用昨日漲跌幅（change_pct=None、量=0）。其餘欄位不變。"""
    uni = build_full_universe(force_refresh)
    snap = _get_live_snapshot()
    if not snap:
        return uni
    quotes = snap.get("quotes") or {}
    try:
        import realtime
        today = realtime.now_tpe().strftime("%Y-%m-%d")
    except Exception:
        return uni
    out = []
    for s in uni:
        q = quotes.get(s.get("code"))
        if not q or q[4] != today or (s.get("quote_date") or "") >= today:
            out.append(s)
            continue
        price, prev, vol = q[0], q[1], q[2]
        s2 = dict(s)
        s2["quote_date"] = today
        s2["live"] = True
        s2["live_time"] = q[3]
        if price and prev:
            s2["close"] = price
            s2["change_pct"] = round((price / prev - 1) * 100, 2)
            s2["volume_lots"] = vol or 0
        else:                                   # 今日尚無成交
            s2["close"] = prev or s2.get("close")
            s2["change_pct"] = None
            s2["volume_lots"] = 0
        out.append(s2)
    return out


_bar_idx = {"key": None, "map": {}}
_mis_bars_cache = {"ts": 0.0, "v": None}


def _official_index() -> Dict[str, Dict]:
    raw = _get_raw_universe()
    key = (raw.get("fetched_at"), len(raw.get("stocks") or []))
    if _bar_idx["key"] != key:
        _bar_idx["map"] = {x.get("code"): x for x in (raw.get("stocks") or []) if x.get("code")}
        _bar_idx["key"] = key
    return _bar_idx["map"]


def _mis_final_bars():
    """盤後（13:33 之後）由證交所 MIS 定案的當日 OHLCV（meta day_bars_mis）；60 秒記憶體快取。"""
    import time as _t
    if _t.time() - _mis_bars_cache["ts"] > 60:
        try:
            from state_store import store
            _mis_bars_cache["v"] = store.get_meta("day_bars_mis") or None
        except Exception:
            _mis_bars_cache["v"] = None
        _mis_bars_cache["ts"] = _t.time()
    return _mis_bars_cache["v"]


def _valid_bar(o, h, l, c) -> bool:
    try:
        return bool(o and h and l and c and l <= min(o, c) + 1e-9 and h >= max(o, c) - 1e-9 and h >= l)
    except Exception:
        return False


def get_official_bar(code: str) -> Optional[Dict]:
    """某檔最新一個交易日的官方日 K（開高低收、成交張數）。
    來源優先序：證交所／櫃買 OpenAPI 日行情（收盤後彙整）→ 證交所 MIS 盤後定案（OpenAPI 還沒更新到今天時用）。
    欄位不全或高低價不合理就不回傳（寧可沿用原資料，也不寫入壞資料）。"""
    bar = None
    try:
        st = _official_index().get(code)
        if st and st.get("quote_date") and _valid_bar(st.get("open"), st.get("high"), st.get("low"), st.get("close")) \
                and st.get("volume_lots") is not None:
            bar = {"date": st["quote_date"], "open": st["open"], "high": st["high"], "low": st["low"],
                   "close": st["close"], "volume": int(st["volume_lots"]), "source": "OpenAPI"}
    except Exception as e:
        logger.warning(f"get_official_bar({code}) OpenAPI: {e}")
    try:
        mb = _mis_final_bars()
        b = (mb or {}).get("bars", {}).get(code)
        if b and (not bar or mb["date"] > bar["date"]) and _valid_bar(b[0], b[1], b[2], b[3]):
            bar = {"date": mb["date"], "open": b[0], "high": b[1], "low": b[2], "close": b[3],
                   "volume": int(b[4] or 0), "source": "MIS"}
    except Exception as e:
        logger.warning(f"get_official_bar({code}) MIS: {e}")
    return bar


_ov_persisted = set()


def _persist_official_volumes(universe):
    """把官方日行情的成交量（張）依日期存起來（每個市場每個交易日只存一次），讓 K 線的成交量用官方數字。"""
    try:
        by = {}
        for s in universe:
            d, m = s.get("quote_date"), s.get("market")
            if d and m and s.get("volume_lots") is not None:
                by.setdefault((m, d), []).append((s["code"], d, s["volume_lots"]))
        from state_store import store
        for k, rows in by.items():
            if k in _ov_persisted or len(rows) < 300:
                continue
            store.upsert_official_volumes(rows)
            _ov_persisted.add(k)
            logger.info(f"官方成交量已存檔：{k[0]} {k[1]} {len(rows)} 檔")
    except Exception as e:
        logger.warning(f"_persist_official_volumes: {e}")


def get_universe_data_meta() -> Dict:
    """給前端標示資料來源／更新時間／產業分類資料本次是否真的抓到，避免使用者
    誤把抓取失敗時的退回值「其他」當成真正的產業分類結果。"""
    raw = _get_raw_universe()
    return {
        "fetched_at": raw.get("fetched_at"),
        "fetch_ok": raw.get("fetch_ok", False),
        "sector_data_ok": bool(raw.get("sector_map")),
        "quote_source": "TWSE OpenAPI STOCK_DAY_ALL（上市）／TPEx OpenAPI tpex_mainboard_quotes（上櫃）",
        "sector_source": "TWSE OpenAPI t187ap03_L（上市）＋TPEx OpenAPI mopsfin_t187ap03_O（上櫃）；查無分類者顯示為「其他」）",
        "scan_universe_threshold": {"min_close": 5, "min_volume_lots": THRESH["min_avg_volume"]},
        # ★ 修正：2026-09-29——原本只回傳一個 quote_trading_date（優先取
        # TWSE 日期）代表「全市場」，但 TWSE／TPEX 兩邊官方資料常常不是同一
        # 天更新（見 _get_raw_universe() 修正說明），這裡額外附上
        # tse_quote_date／otc_quote_date／dates_mismatch，讓前端能分別標示
        # 上市/上櫃各自的資料日期，兩邊不同天時明確示警，而不是只顯示一個
        # 容易誤導的單一日期。這份快照實際上是「哪一個交易日」收盤後的成交量
        # /價格資料（不是我們的抓取時間，是 TWSE/TPEX 官方資料本身標示的交易
        # 日）。TWSE 官方 API 本身只在收盤後才會更新當天資料，如果現在還沒收盤
        # 、或官方資料還沒更新完，這裡看到的會是上一個已收盤交易日，這是正常
        # 現象，不是成交量算錯了——把這個日期清楚顯示出來，讓使用者自己判斷
        # 看到的數字是不是「今天」的。
        "quote_trading_date": raw.get("quote_trading_date"),
        "tse_quote_date": raw.get("tse_quote_date"),
        "otc_quote_date": raw.get("otc_quote_date"),
        "dates_mismatch": raw.get("dates_mismatch", False),
        "intraday": live_status(),
    }

def get_scan_batches(batch_size=None) -> List[List[str]]:
    batch_size = batch_size or SYSTEM["scan_batch_size"]
    tickers = [s["ticker"] for s in build_universe()]
    return [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

def get_stock_info(ticker: str) -> Optional[Dict]:
    """查掃描池（受 200張/5元門檻限制）。scanner.py／backtester.py 用這個——
    只需要知道候選股票的 size_cat 等資訊，本來就該侷限在掃描池範圍內。"""
    universe = build_universe()
    for s in universe:
        if s["ticker"] == ticker:
            return s
    return None

def get_stock_info_any(ticker: str) -> Optional[Dict]:
    """★ 新增：2026-09-29——查全部上市櫃股票（不受掃描池門檻限制），供個股
    研究頁查詢用。跟 get_stock_info() 的差別只在資料來源：這個查
    build_full_universe()。"""
    for s in build_full_universe():
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


# ★ 新增：2026-09-26——使用者要調整 THRESH["min_avg_volume"]（目前 500 張）這個
# 全市場掃描的流動性門檻，但在改之前想先知道「調到多少張，會多納入幾檔」。
# 這裡提供一個唯讀診斷函式：抓一次全市場原始清單（跳過 sector 分類，因為這裡
# 只是要算門檔分布，不需要產業資訊，可以省掉一次 API 呼叫加快回應），只套用
# close>5 這條（跟正式流程一致，避免雞蛋水餃股混進統計），然後回傳在幾個候選
# 門檻值下各自會納入幾檔，讓使用者能在真正修改 config.py 前先看到影響範圍。
def get_volume_threshold_distribution(candidate_thresholds=None) -> Dict:
    candidate_thresholds = candidate_thresholds or [500, 300, 200, 150, 100, 50, 0]
    tse = _fetch_twse_list()
    tpex = _fetch_tpex_list()
    all_stocks = tse + tpex
    priced = [s for s in all_stocks if s["close"] > 5]
    result = {
        "raw_total_tse": len(tse),
        "raw_total_tpex": len(tpex),
        "raw_total_after_price_filter": len(priced),
        "current_threshold": THRESH["min_avg_volume"],
        "by_threshold": {},
    }
    for t in candidate_thresholds:
        n = sum(1 for s in priced if s["volume_lots"] >= t)
        result["by_threshold"][str(t)] = n
    return result
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

# ★ 新增：2026-09-29——市場總覽頁要的「漲跌家數」「產業排行」，都從
# build_universe() 既有快取（每日更新一次）算出來，不額外打 API、不用即時
# 對全市場重抓。change_pct 是 None 的股票（見 _parse_change() 說明：欄位
# 解析失敗，或這次 TWSE/TPEX 清單抓取本身失敗）一律不計入漲/跌/平家數，
# 避免用猜的數字冒充真的漲跌方向。
# ★ 修正：2026-09-29——使用者反映查詢/統計不該被限制在掃描池的200張成交量
# 門檻以內。原本這三個函式（漲跌家數、產業排行、股票篩選器）都是用
# build_universe()（掃描池，套門檻）算的，代表「上漲家數/下跌家數」這類市場
# 總覽統計，還有篩選器能查到的股票，其實都只涵蓋成交量>=200張的股票，跟頁面
# 標題「市場總覽」（應該代表全市場）不符，也不夠準確。全部改用不設門檻的
# build_full_universe()，涵蓋全部上市＋上櫃股票。
def get_market_breadth() -> Dict:
    universe = build_live_universe()
    covered = [s for s in universe if s.get("change_pct") is not None]
    up = [s for s in covered if s["change_pct"] > 0]
    down = [s for s in covered if s["change_pct"] < 0]
    flat = [s for s in covered if s["change_pct"] == 0]
    limit_up = [s for s in up if s["change_pct"] >= 9.5]
    limit_down = [s for s in down if s["change_pct"] <= -9.5]

    # ★ 修正：2026-09-29——使用者回報「市場總覽的上漲加速及下跌加速全部加
    # 起來的數字其實都有錯」。根因（詳見 _get_raw_universe() 修正說明）：
    # 上面 advancers/decliners 是把上市(TSE)＋上櫃(OTC)兩個交易所的股票直接
    # 混在一起算，但這兩邊的官方資料目前常常不是同一個交易日（TWSE 尚未補
    # 上假期後最新收盤、TPEX 已經是今天的資料），等於把「兩個不同交易日」
    # 的漲跌家數加在一起，總數本身失去意義。這裡分開統計 TSE／OTC 各自的
    # 漲跌家數與各自的資料日期，讓前端可以分別顯示正確、同一天的統計數字，
    # 並在兩邊日期不一致時明確示警——不拿掉原本的合計欄位（避免破壞既有
    # 前端相容性），但合計不再是唯一可信的數字。
    def _breadth_for(group):
        cov = [s for s in group if s.get("change_pct") is not None]
        u = [s for s in cov if s["change_pct"] > 0]
        d = [s for s in cov if s["change_pct"] < 0]
        f = [s for s in cov if s["change_pct"] == 0]
        lu = [s for s in u if s["change_pct"] >= 9.5]
        ld = [s for s in d if s["change_pct"] <= -9.5]
        dates = sorted({s["quote_date"] for s in group if s.get("quote_date")})
        return {
            "total": len(group), "covered": len(cov),
            "advancers": len(u), "decliners": len(d), "unchanged": len(f),
            "limit_up": len(lu), "limit_down": len(ld),
            "quote_date": dates[-1] if dates else None,
        }
    tse_group = [s for s in universe if s.get("market") == "TSE"]
    otc_group = [s for s in universe if s.get("market") == "OTC"]
    tse_breadth = _breadth_for(tse_group)
    otc_breadth = _breadth_for(otc_group)
    dates_mismatch = bool(tse_breadth["quote_date"] and otc_breadth["quote_date"]
                           and tse_breadth["quote_date"] != otc_breadth["quote_date"])

    return {
        "total": len(universe), "covered": len(covered),
        "advancers": len(up), "decliners": len(down), "unchanged": len(flat),
        "limit_up": len(limit_up), "limit_down": len(limit_down),
        "coverage_ok": len(covered) > 0,
        "tse": tse_breadth, "otc": otc_breadth,
        "dates_mismatch": dates_mismatch,
    }

def get_sector_performance() -> List[Dict]:
    universe = build_live_universe()
    by_sector: Dict[str, List[float]] = {}
    for s in universe:
        cp = s.get("change_pct")
        if cp is None: continue
        sec = s.get("sector") or "未分類"
        by_sector.setdefault(sec, []).append(cp)
    out = []
    for sec, chgs in by_sector.items():
        out.append({
            "sector": sec, "n": len(chgs),
            "avg_change_pct": round(sum(chgs)/len(chgs), 2),
            "up": sum(1 for c in chgs if c > 0), "down": sum(1 for c in chgs if c < 0),
        })
    out.sort(key=lambda x: x["avg_change_pct"], reverse=True)
    return out

def screen_universe(filters: Dict) -> List[Dict]:
    """市場總覽頁的股票篩選器。只用既有欄位篩（代號/名稱/產業/市值分類/價格/
    漲跌幅/成交量），不做 RSI/MACD/均線這類需要對全市場即時算技術指標的篩選
    ——現有掃描架構只對「候選訊號」的幾十檔股票算技術指標（見 scanner.py），
    對全市場 1000+ 檔即時算的成本太高，這裡先不做，避免每次篩選都變成一次
    重量級全市場計算。
    ★ 修正：2026-09-29——改用不設門檻的 build_full_universe()，查詢範圍涵蓋
    全部上市＋上櫃股票，不再受 build_universe() 的200張成交量門檻限制。"""
    universe = build_live_universe()
    q = (filters.get("q") or "").strip().lower()
    sector = filters.get("sector") or ""
    size_cat = filters.get("size_cat") or ""
    min_price = filters.get("min_price"); max_price = filters.get("max_price")
    min_chg = filters.get("min_chg_pct"); max_chg = filters.get("max_chg_pct")
    min_vol = filters.get("min_volume_lots")
    out = []
    for s in universe:
        if q and q not in s.get("code","").lower() and q not in s.get("name","").lower(): continue
        if sector and s.get("sector") != sector: continue
        if size_cat and s.get("size_cat") != size_cat: continue
        if min_price is not None and s.get("close",0) < min_price: continue
        if max_price is not None and s.get("close",0) > max_price: continue
        cp = s.get("change_pct")
        if min_chg is not None and (cp is None or cp < min_chg): continue
        if max_chg is not None and (cp is None or cp > max_chg): continue
        if min_vol is not None and s.get("volume_lots",0) < min_vol: continue
        out.append(s)
    return out
