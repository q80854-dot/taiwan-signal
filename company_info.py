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
def _build_foreign_holding_map() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    r = requests.get(TWSE_QFIIS_URL, headers=HEADERS, timeout=20)
    if not r.ok:
        return out
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return out
    sample = rows[0]
    k_code = _find_key(sample, "證券代號", "Code", "代號")
    k_ratio = _find_key(sample, "全體外資及陸資持股比率", "持股比率", "shares held", "Percentage of shares")
    k_ceil = _find_key(sample, "投資上限比率", "上限", "available investment", "ceiling")
    k_held = _find_key(sample, "全體外資及陸資持有股數", "持有股數", "shares held", "number of shares")
    if not k_code or not k_ratio:
        return out
    for row in rows:
        code = str(row.get(k_code, "")).strip()
        if not code or not code.isdigit():
            continue
        out[code] = {
            "ratio": _num(row.get(k_ratio)),
            "ceiling": _num(row.get(k_ceil)) if k_ceil else None,
            "held_shares": _num(row.get(k_held)) if k_held else None,
        }
    return out


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
