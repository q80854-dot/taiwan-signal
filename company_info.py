"""個股研究的延伸資料：公司概況、外資（機構）持股比率、集保股權分散（大戶／散戶）。

全部使用免費官方／公開來源，依代號建全市場對照表、每日快取，單檔查詢只讀快取：
- 公司概況：TWSE OpenAPI t187ap03_L（上市）＋ TPEx mopsfin_t187ap03_O（上櫃）
- 外資及陸資持股比率：TWSE OpenAPI fund/MI_QFIIS（僅上市；上櫃官方無對應免費逐檔端點）
- 集保股權分散（大戶持股）：TDCC 集保戶股權分散表 OpenData（id=1-5，全市場、每週）

設計原則：
- 欄位用「關鍵字比對」而非寫死（官方中英文欄位名偶有差異），來源改版較不易整個壞掉。
- 任一來源失敗只影響該區塊，回傳 None／空，不丟例外；呼叫端已有優雅降級。
- 逐檔「前五大法人名單」「內部人逐筆交易」「分析師評級」台股沒有穩定的免費逐檔
  API，故本模組不提供（個股研究頁會明白標示未提供與原因），不以推估或假資料充數。
"""
import csv
import io
import logging
import threading
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

TWSE_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_INFO_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_QFIIS_URL = "https://openapi.twse.com.tw/v1/fund/MI_QFIIS"
TDCC_DISPERSION_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"

_TTL = 12 * 3600
_cache = {}            # name -> {"at": ts, "data": any}
_locks = {}
_locks_guard = threading.Lock()


def _lock(name):
    with _locks_guard:
        lk = _locks.get(name)
        if lk is None:
            lk = _locks[name] = threading.Lock()
        return lk


def _cached(name, builder):
    ent = _cache.get(name)
    if ent and time.time() - ent["at"] < _TTL:
        return ent["data"]
    with _lock(name):
        ent = _cache.get(name)
        if ent and time.time() - ent["at"] < _TTL:
            return ent["data"]
        try:
            data = builder()
        except Exception as e:
            logger.warning(f"company_info {name}: {e}")
            data = (ent or {}).get("data")          # 失敗時沿用舊快取（若有）
        _cache[name] = {"at": time.time(), "data": data}
        return data


def _find_key(d: dict, *subs):
    """在 dict 的 key 裡找第一個包含任一子字串的 key（不分大小寫），容忍官方欄位改名。"""
    for k in d.keys():
        ks = str(k)
        for s in subs:
            if s in ks:
                return k
    return None


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


# ───────────────────────── 公司概況 ─────────────────────────
def _build_profile_map() -> Dict[str, dict]:
    out: Dict[str, dict] = {}

    def ingest(rows, market):
        if not isinstance(rows, list) or not rows:
            return
        sample = rows[0]
        k_code = _find_key(sample, "公司代號", "SecuritiesCompanyCode", "Code", "代號")
        if not k_code:
            return
        k_name = _find_key(sample, "公司簡稱", "公司名稱", "CompanyName", "簡稱", "名稱")
        k_ind = _find_key(sample, "產業別", "Industry")
        k_est = _find_key(sample, "成立日期", "Establishment")
        k_list = _find_key(sample, "上市日期", "上櫃日期", "興櫃", "ListingDate", "掛牌")
        k_cap = _find_key(sample, "實收資本額", "Capital", "資本額")
        k_chair = _find_key(sample, "董事長", "Chairman")
        k_ceo = _find_key(sample, "總經理", "GeneralManager", "President")
        k_spk = _find_key(sample, "發言人", "Spokesman")
        k_web = _find_key(sample, "網址", "WebSite", "Website", "URL")
        k_biz = _find_key(sample, "主要經營業務", "營業項目", "經營業務", "Business")
        for r in rows:
            code = str(r.get(k_code, "")).strip()
            if not code or not code.isdigit():
                continue
            out[code] = {
                "code": code, "market": market,
                "name": str(r.get(k_name, "")).strip() if k_name else None,
                "industry": str(r.get(k_ind, "")).strip() if k_ind else None,
                "established": str(r.get(k_est, "")).strip() if k_est else None,
                "listed": str(r.get(k_list, "")).strip() if k_list else None,
                "capital": _num(r.get(k_cap)) if k_cap else None,
                "chairman": str(r.get(k_chair, "")).strip() if k_chair else None,
                "ceo": str(r.get(k_ceo, "")).strip() if k_ceo else None,
                "spokesman": str(r.get(k_spk, "")).strip() if k_spk else None,
                "website": str(r.get(k_web, "")).strip() if k_web else None,
                "business": (str(r.get(k_biz, "")).strip() or None) if k_biz else None,
            }

    try:
        r = requests.get(TWSE_INFO_URL, headers=HEADERS, timeout=15)
        if r.ok:
            ingest(r.json(), "TSE")
    except Exception as e:
        logger.warning(f"profile TWSE: {e}")
    try:
        r = requests.get(TPEX_INFO_URL, headers=HEADERS, timeout=20)
        if r.ok:
            ingest(r.json(), "OTC")
    except Exception as e:
        logger.warning(f"profile TPEx: {e}")
    return out


def get_profile(code: str) -> Optional[dict]:
    return (_cached("profile", _build_profile_map) or {}).get(str(code).strip())


# ──────────────────── 外資及陸資持股比率（機構持比） ────────────────────
# 修正（2026-10-11）：原本打 openapi.twse.com.tw/v1/fund/MI_QFIIS（dict 陣列）在
# 正式站取不到資料。逐檔外資持股比率的正式來源是 TWSE「報表」端點
# www.twse.com.tw/fund/MI_QFIIS?response=json&selectType=ALL，回應為
# {stat, date, fields:[欄名...], data:[[值...], ...]} 的「表頭＋列陣列」格式
# （與 T86／MI_MARGN 同一家族，程式他處已證實可從 Render 取得）。改用 fields
# 動態定位欄位（不寫死索引），比較不怕官方調整欄序。
def _col_idx(fields, *subs, exclude=()):
    for i, f in enumerate(fields):
        fs = str(f)
        if any(s in fs for s in subs) and not any(x in fs for x in exclude):
            return i
    return None


def _build_foreign_holding_map() -> dict:
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(hours=8)
    for back in range(0, 8):                       # 今天往前試幾個交易日（假日／尚未公布時往回找）
        d = now - __import__("datetime").timedelta(days=back)
        ds = d.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/fund/MI_QFIIS?response=json&selectType=ALL&date={ds}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=(5, 30))
            if not r.ok:
                continue
            j = r.json()
            if j.get("stat") != "OK" or not j.get("data"):
                continue
            fields = j.get("fields") or []
            ci = _col_idx(fields, "證券代號", "代號", "Code")
            ri = _col_idx(fields, "持股比率", "Percentage of shares", exclude=("可投資", "尚可", "available"))
            cei = _col_idx(fields, "上限", "available investment")
            hi = _col_idx(fields, "持有股數", "number of shares", exclude=("可投資", "尚可", "available"))
            if ci is None or ri is None:
                ci = 0 if ci is None else ci   # 退而求其次：代號多半在第 0 欄
                if ri is None:
                    continue
            m = {}
            for row in j["data"]:
                try:
                    code = str(row[ci]).strip()
                    if not code or not code.isdigit():
                        continue
                    m[code] = {"ratio": _num(row[ri]),
                               "ceiling": _num(row[cei]) if cei is not None and cei < len(row) else None,
                               "held_shares": _num(row[hi]) if hi is not None and hi < len(row) else None,
                               "date": j.get("date") or ds}
                except Exception:
                    continue
            if m:
                return m
        except Exception as e:
            logger.warning(f"foreign_holding {ds}: {e}")
    return {}


def get_foreign_holding(code: str) -> Optional[dict]:
    return (_cached("qfiis", _build_foreign_holding_map) or {}).get(str(code).strip())


# ──────────────────── 集保股權分散（大戶／散戶持股） ────────────────────
# TDCC 持股分級（以「股」為界，1 張＝1,000 股）：
#   1:1–999、2:1,000–5,000 … 11:200,001–400,000、12:400,001–600,000、
#   13:600,001–800,000、14:800,001–1,000,000、15:1,000,001 以上、
#   16:差異數調整、17:合計
# 大戶＝≥400 張（level 12–15）；散戶＝≤10 張（level 1–3：<999、1–5 張、5–10 張）
_BIG_LEVELS = {"12", "13", "14", "15"}
_SMALL_LEVELS = {"1", "2", "3"}


def _build_dispersion_map() -> dict:
    r = requests.get(TDCC_DISPERSION_URL, headers=HEADERS, timeout=30)
    if not r.ok:
        return {"date": None, "map": {}}
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    agg: Dict[str, dict] = {}
    data_date = None
    for row in reader:
        kd = _find_key(row, "資料日期", "date")
        kc = _find_key(row, "證券代號", "代號", "code")
        kl = _find_key(row, "持股分級", "分級", "level")
        kp = _find_key(row, "占集保庫存數比例", "比例", "percent", "占")
        kh = _find_key(row, "人數", "holder", "people")
        if not (kc and kl and kp):
            break
        code = str(row.get(kc, "")).strip()
        lvl = str(row.get(kl, "")).strip()
        pct = _num(row.get(kp))
        if not code or pct is None:
            continue
        data_date = data_date or (str(row.get(kd, "")).strip() if kd else None)
        a = agg.setdefault(code, {"big": 0.0, "small": 0.0, "holders": None})
        if lvl in _BIG_LEVELS:
            a["big"] += pct
        elif lvl in _SMALL_LEVELS:
            a["small"] += pct
        if lvl == "17" and kh:          # 合計列帶總股東人數
            a["holders"] = _num(row.get(kh))
    for code, a in agg.items():
        a["big"] = round(a["big"], 2)
        a["small"] = round(a["small"], 2)
    return {"date": data_date, "map": agg}


def get_dispersion(code: str) -> Optional[dict]:
    d = _cached("tdcc", _build_dispersion_map) or {}
    row = (d.get("map") or {}).get(str(code).strip())
    if not row:
        return None
    return {"date": d.get("date"), "big_pct": row.get("big"),
            "small_pct": row.get("small"), "holders": row.get("holders")}


# ──────────────────── 董監事持股（內部人持股） ────────────────────
# 來源：TWSE/TPEx opendata 董監事持股相關彙總表（t187ap11）。
# 誠實註記：官方免費逐檔「全體董監持股比率」僅此類彙總表較接近，且主要涵蓋
# 「持股不足法定成數」或當期揭露之公司，未必每檔都有；取不到就整塊不顯示（不以假資料充數）。
# 沙箱無法連 TWSE 實測，正式站與「資料來源連線檢測」面板可確認是否取得。
TWSE_BOARD_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap11_L"
TPEX_BOARD_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap11_O"


def _build_board_holding_map() -> Dict[str, dict]:
    out: Dict[str, dict] = {}

    def ingest(rows):
        if not isinstance(rows, list) or not rows:
            return
        s = rows[0]
        k_code = _find_key(s, "公司代號", "證券代號", "代號", "Code")
        if not k_code:
            return
        k_issued = _find_key(s, "發行股數", "已發行", "issued")
        # 直接的「成數／比率」欄位（實際、目前），排除法定應持有
        k_ratio = _find_key(s, "持股成數", "持股比率", "percentage")
        if k_ratio and any(x in str(k_ratio) for x in ("法定", "應")):
            k_ratio = None
        # 董事、監察人持股（可能分開，也可能已有合計）
        k_all = _find_key(s, "全體董監", "董監事持股", "董監持股")
        k_dir = _find_key(s, "全體董事持股", "董事持股", "董事目前")
        k_sup = _find_key(s, "全體監察人持股", "監察人持股", "監察人目前")
        k_pledge = _find_key(s, "設質", "質押", "pledge")
        for r in rows:
            code = str(r.get(k_code, "")).strip()
            if not code or not code.isdigit():
                continue
            shares = None
            if k_all:
                shares = _num(r.get(k_all))
            elif k_dir or k_sup:
                shares = (_num(r.get(k_dir)) or 0) + (_num(r.get(k_sup)) or 0)
            issued = _num(r.get(k_issued)) if k_issued else None
            ratio = _num(r.get(k_ratio)) if k_ratio else None
            if ratio is None and shares and issued:
                ratio = round(shares / issued * 100, 2)
            if ratio is None and shares is None:
                continue
            out[code] = {"ratio": ratio, "shares": shares, "issued": issued,
                         "pledge_pct": _num(r.get(k_pledge)) if k_pledge else None}
    for url, label in ((TWSE_BOARD_URL, "TWSE"), (TPEX_BOARD_URL, "TPEx")):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.ok:
                ingest(r.json())
        except Exception as e:
            logger.warning(f"board_holding {label}: {e}")
    return out


def get_board_holding(code: str) -> Optional[dict]:
    return (_cached("board", _build_board_holding_map) or {}).get(str(code).strip())
