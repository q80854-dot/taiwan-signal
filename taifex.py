"""期交所（TAIFEX）籌碼：三大法人期貨／選擇權未平倉、Put/Call 比、大額交易人。

來源：https://openapi.taifex.com.tw/v1 （2026-10-07 以瀏覽器實測欄位）
  /MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate  三大法人各期貨契約（ContractCode 中文名、Item=自營商/投信/外資及陸資）
  /MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate      三大法人臺指選擇權 CALL／PUT
  /PutCallRatio                                                             臺指選擇權 Put/Call 比（近 20 日）
  /OpenInterestOfLargeTradersFutures                                        大額交易人未沖銷部位（Contract=TX）
只做「顯示與紀錄」，不直接影響訊號分數（沒有驗證前不加規則）。
"""
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)
BASE = "https://openapi.taifex.com.tw/v1"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_cache = {"ts": 0.0, "v": None}
_lock = threading.Lock()
_TTL = 1800

# 小型臺指 = 臺股期貨 1/4、微型臺指 = 1/20；折算成「大台等效口數」才能把外資部位加總
_EQ = {"臺股期貨": 1.0, "小型臺指期貨": 0.25, "微型臺指期貨": 0.05}


def _num(x) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _iso(d) -> Optional[str]:
    d = str(d or "")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else None


def _get(path: str):
    r = requests.get(BASE + path, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def parse_futures(rows: List[Dict]) -> Dict:
    """→ {date, items:{外資及陸資/投信/自營商:{tx_net_oi, equiv_net_oi, long, short}}}（口數）。"""
    date, items = None, {}
    for r in rows or []:
        c, who = r.get("ContractCode"), r.get("Item")
        if c not in _EQ or not who:
            continue
        date = date or _iso(r.get("Date"))
        net, lo, sh = _num(r.get("OpenInterest(Net)")), _num(r.get("OpenInterest(Long)")), _num(r.get("OpenInterest(Short)"))
        if net is None:
            continue
        it = items.setdefault(who, {"tx_net_oi": 0.0, "equiv_net_oi": 0.0, "tx_long": 0.0, "tx_short": 0.0})
        it["equiv_net_oi"] += net * _EQ[c]
        if c == "臺股期貨":
            it["tx_net_oi"] = net
            it["tx_long"], it["tx_short"] = lo or 0, sh or 0
    for it in items.values():
        it["equiv_net_oi"] = round(it["equiv_net_oi"])
    return {"date": date, "items": items}


def parse_options(rows: List[Dict]) -> Dict:
    """→ {who: {call_net_oi, put_net_oi}}（臺指選擇權，未平倉淨口數）。"""
    out = {}
    for r in rows or []:
        if r.get("ContractCode") != "臺指選擇權":
            continue
        who, cp = r.get("Item"), str(r.get("CallPut", "")).upper()
        v = _num(r.get("OpenInterest(Net)"))
        if who and cp in ("CALL", "PUT") and v is not None:
            out.setdefault(who, {})[cp.lower() + "_net_oi"] = v
    return out


def parse_pcr(rows: List[Dict]) -> List[Dict]:
    out = []
    for r in rows or []:
        d, oi, vol = _iso(r.get("Date")), _num(r.get("PutCallOIRatio%")), _num(r.get("PutCallVolumeRatio%"))
        if d and oi is not None:
            out.append({"date": d, "oi_ratio": oi, "vol_ratio": vol})
    return sorted(out, key=lambda x: x["date"])


def parse_large(rows: List[Dict]) -> Optional[Dict]:
    """臺股期貨（TX）全部月份（999912）、全部交易人（TypeOfTraders=0）的前五／前十大買賣與市場未平倉。"""
    for r in rows or []:
        if r.get("Contract") == "TX" and str(r.get("SettlementMonth")) == "999912" and str(r.get("TypeOfTraders")) == "0":
            b5, s5, b10, s10, oi = (_num(r.get(k)) for k in ("Top5Buy", "Top5Sell", "Top10Buy", "Top10Sell", "OIOfMarket"))
            if None in (b5, s5, b10, s10) or not oi:
                return None
            return {"top5_net": b5 - s5, "top10_net": b10 - s10, "market_oi": oi,
                    "top10_net_pct": round((b10 - s10) / oi * 100, 1)}
    return None


def fetch_taifex() -> Dict:
    out = {"at": time.time(), "errors": []}
    for key, path, fn in (("futures", "/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate", parse_futures),
                          ("options", "/MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate", parse_options),
                          ("pcr", "/PutCallRatio", parse_pcr), ("large", "/OpenInterestOfLargeTradersFutures", parse_large)):
        try:
            out[key] = fn(_get(path))
        except Exception as e:
            out["errors"].append(f"{key}: {e}")
            logger.warning(f"taifex {key}: {e}")
    return out


def get_taifex(force: bool = False) -> Dict:
    with _lock:
        if not force and _cache["v"] and time.time() - _cache["ts"] < _TTL:
            return _cache["v"]
    v = merge_keep_last(fetch_taifex(), _cache["v"])
    with _lock:
        _cache["v"], _cache["ts"] = v, time.time()
    return v


def merge_keep_last(new: Dict, old: Optional[Dict]) -> Dict:
    """某個來源這次抓失敗（例如期交所收盤後資料重發佈、回傳空內容）就沿用上一次成功的值，並標記 stale。"""
    new["stale"] = []
    if not old:
        return new
    for k in ("futures", "options", "pcr", "large"):
        bad = k not in new or not new.get(k) or (k == "futures" and not (new.get(k) or {}).get("items"))
        if bad and old.get(k):
            new[k] = old[k]
            new["stale"].append(k)
    return new


def update_hist(store, data: Optional[Dict] = None) -> Optional[str]:
    """把當日外資／投信／自營商大台等效淨未平倉與 P/C 比記進 meta taifex_hist（保留 40 日），回傳日期。"""
    data = data or get_taifex()
    fut = data.get("futures") or {}
    d = fut.get("date")
    if not d or not fut.get("items"):
        return None
    try:
        store.set_meta("taifex_last", {k: data.get(k) for k in ("futures", "options", "pcr", "large")})
    except Exception:
        pass
    hist = store.get_meta("taifex_hist") or {}
    pcr = next((p for p in reversed((data.get("pcr") or [])) if p["date"] == d), None)
    hist[d] = {"equiv": {k: v["equiv_net_oi"] for k, v in fut["items"].items()}, "pcr_oi": pcr["oi_ratio"] if pcr else None}
    for k in sorted(hist)[:-40]:
        hist.pop(k, None)
    store.set_meta("taifex_hist", hist)
    return d


def summary(store=None) -> Dict:
    """給網頁用：最新一日數字＋ 5 日變化＋白話判讀（只描述，不下買賣建議）。"""
    data = get_taifex()
    if not (data.get("futures") or {}).get("items") and store is not None:
        try:   # 重啟後記憶體快取是空的、來源又剛好暫時失敗：改用資料庫裡最後一次成功的值
            last = store.get_meta("taifex_last") or {}
            if (last.get("futures") or {}).get("items"):
                data = dict(data, **{k: last[k] for k in last if last[k]}, stale=["futures"])
        except Exception:
            pass
    fut, pcr = data.get("futures") or {}, data.get("pcr") or []
    out = {"date": fut.get("date"), "errors": data.get("errors", []), "stale": data.get("stale", []), "items": fut.get("items", {}),
           "options": data.get("options"), "large": data.get("large"), "pcr": pcr[-1] if pcr else None}
    if len(pcr) >= 6:
        out["pcr_chg_5d"] = round(pcr[-1]["oi_ratio"] - pcr[-6]["oi_ratio"], 2)
    f = (fut.get("items") or {}).get("外資及陸資")
    if f:
        out["foreign_equiv_net_oi"] = f["equiv_net_oi"]
        try:
            hist = (store.get_meta("taifex_hist") or {}) if store else {}
            ds = sorted(d for d in hist if d < (fut.get("date") or "9"))
            if len(ds) >= 5 and "外資及陸資" in hist[ds[-5]]["equiv"]:
                out["foreign_chg_5d"] = f["equiv_net_oi"] - hist[ds[-5]]["equiv"]["外資及陸資"]
        except Exception:
            pass
        n = f["equiv_net_oi"]
        out["foreign_note"] = ("外資大台等效淨空單，期貨端偏保守" if n <= -30000 else
                               "外資大台等效淨多單，期貨端偏積極" if n >= 30000 else "外資期貨部位接近中性")
    return out
