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


def _get(path: str, tries: int = 3):
    """期交所偶爾回 200 但內容是空的（資料重發佈時段），重試幾次再放棄；失敗訊息帶狀態碼與長度方便查。"""
    last = None
    for i in range(tries):
        try:
            r = requests.get(BASE + path, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if not r.content.strip():
                raise RuntimeError(f"空回應（HTTP 200, {len(r.content)} bytes）")
            try:
                return r.json()
            except ValueError:
                raise RuntimeError(f"非 JSON 回應（{r.headers.get('Content-Type', '?')}，開頭：{r.text[:60]!r}）")
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise last


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


WEB = "https://www.taifex.com.tw/cht/3/futContractsDateDown"


def _tpe_now() -> datetime:
    from datetime import timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(hours=8)


def parse_futures_csv(text: str) -> Dict:
    """期交所網站「三大法人－區分各期貨契約」CSV 下載（OpenAPI 失效或還沒更新時的備援）。
    欄位依標題文字對應：日期、商品名稱、身份別、多方未平倉口數、空方未平倉口數、多空未平倉口數淨額。"""
    import csv, io
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if not rows:
        return {"date": None, "items": {}}
    head = [h.strip() for h in rows[0]]

    def col(*keys):
        for i, h in enumerate(head):
            if all(k in h for k in keys):
                return i
        return None
    i_d, i_c, i_w = col("日期"), col("商品"), col("身份")
    i_lo, i_sh, i_net = col("多方未平倉口數"), col("空方未平倉口數"), col("未平倉口數淨額")
    if None in (i_d, i_c, i_w, i_net):
        raise RuntimeError(f"CSV 欄位無法辨識：{head[:8]}")
    conv = []
    for r in rows[1:]:
        if len(r) <= max(i_d, i_c, i_w, i_net):
            continue
        who = r[i_w].strip()
        who = "外資及陸資" if "外資" in who else who
        d = r[i_d].strip().replace("/", "")
        conv.append({"Date": d, "ContractCode": r[i_c].strip(), "Item": who,
                     "OpenInterest(Net)": r[i_net], "OpenInterest(Long)": r[i_lo] if i_lo is not None else None,
                     "OpenInterest(Short)": r[i_sh] if i_sh is not None else None})
    return parse_futures(conv)


def fetch_futures_web(day: Optional[datetime] = None) -> Dict:
    d = (day or _expected_dt()).strftime("%Y/%m/%d")
    r = requests.post(WEB, data={"queryStartDate": d, "queryEndDate": d, "commodityId": ""},
                      headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20)
    if r.status_code != 200 or not r.content.strip():
        raise RuntimeError(f"期交所網站 HTTP {r.status_code}")
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            text = r.content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError("期交所網站 CSV 無法解碼")
    return parse_futures_csv(text)


def _expected_dt(now: Optional[datetime] = None) -> datetime:
    """三大法人「現在應該至少有哪一天」的交易日：平日 15:00 後＝今天，否則上一個平日。
    （不判斷國定假日；假日時官方沒有資料，頂多多查幾次、成本很低。）"""
    from datetime import timedelta
    d = now or _tpe_now()
    if not (d.weekday() <= 4 and d.hour >= 15):
        d = d - timedelta(days=1)
        while d.weekday() > 4:
            d -= timedelta(days=1)
    return d


def _expected_date(now: Optional[datetime] = None) -> str:
    return _expected_dt(now).strftime("%Y-%m-%d")


def _need_newer(date: Optional[str], now: Optional[datetime] = None) -> bool:
    """資料日期比「應有的最新交易日」舊 → 需要找更新的來源（期交所日盤約 14:30～15:00 後公布三大法人）。
    （2026-10-09：原本限定『平日 15:00 後』才成立，過了午夜就恆為 False，使收盤後只拿到 OpenAPI 的舊日期、
    不再改抓已更新的網站 CSV——落後的資料要等到隔天 15:00 才會自我修復。改成直接和應有交易日比較。）"""
    return (date or "") < _expected_date(now)


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
    fut = out.get("futures") or {}
    if not fut.get("items") or _need_newer(fut.get("date")):
        # OpenAPI 失敗或還停在前一天：改抓期交所網站 CSV。查「應有的交易日」而非今天，
        # 並在該日無資料（例如假日）時往前退一個平日再試一次，避免過了午夜後查到空的今天。
        from datetime import timedelta
        day = _expected_dt()
        for _ in range(2):
            try:
                web = fetch_futures_web(day)
                if web.get("items") and (web.get("date") or "") > (fut.get("date") or ""):
                    out["futures"] = web
                    out["futures_source"] = "期交所網站 CSV"
                    logger.info(f"taifex futures：OpenAPI 無新資料，改用網站 CSV（{web.get('date')}）")
                    break
                if web.get("items"):
                    break                       # 有資料但不比現有新：不用再往前找
            except Exception as e:
                out["errors"].append(f"futures_web: {e}")
                logger.warning(f"taifex futures_web({day:%Y-%m-%d}): {e}")
            day -= timedelta(days=1)
            while day.weekday() > 4:
                day -= timedelta(days=1)
    return out


def get_taifex(force: bool = False) -> Dict:
    with _lock:
        ttl = 180 if _cache["v"] and _need_newer(((_cache["v"].get("futures") or {}).get("date"))) else _TTL
        if not force and _cache["v"] and time.time() - _cache["ts"] < ttl:
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
