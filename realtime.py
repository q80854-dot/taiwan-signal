"""盤中即時報價（官方來源優先）。

來源順序：
  1. 證交所「基本市況報導網站」MIS（mis.twse.com.tw getStockInfo）——上市與上櫃都走這支，官方盤中即時成交價
  2. 富果 Fugle 即時行情 API（需 FUGLE_API_KEY；富邦證券旗下授權行情商）——MIS 抓不到時備援
都抓不到就回 None，由呼叫端明確告知「抓不到」，不用舊資料假裝即時。

盤中（09:00–13:35 台北時間）每 5 分鐘由 app.job_intraday_snapshot() 把掃描池全市場快照存進資料庫
meta["intraday_snapshot"]，個股頁另外即時查單一檔（10 秒快取）。
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pytz
import requests

logger = logging.getLogger(__name__)

TZ = pytz.timezone("Asia/Taipei")
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
FUGLE_BASE = "https://api.fugle.tw/marketdata/v1.0"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Referer": MIS_HOME, "Accept": "application/json, text/plain, */*"}
BATCH = 50

_lock = threading.Lock()
_session: Optional[requests.Session] = None
_session_at = 0.0
_cache: Dict[str, tuple] = {}          # code -> (ts, quote)
_CACHE_TTL = 10
_stats = {"mis_ok": 0, "mis_fail": 0, "fugle_ok": 0, "last_error": None, "last_ok_at": None}


def now_tpe() -> datetime:
    return datetime.now(TZ)


def market_open(dt: Optional[datetime] = None) -> bool:
    """平日 09:00–13:35（含收盤後五分鐘，等最後一盤）。不判斷國定假日，假日時 MIS 的 d 欄位日期不是今天，會被標成非即時。"""
    dt = dt or now_tpe()
    if dt.weekday() > 4:
        return False
    m = dt.hour * 60 + dt.minute
    return 9 * 60 <= m <= 13 * 60 + 35


def _num(x) -> Optional[float]:
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else None
    except Exception:
        return None


def _get_session() -> requests.Session:
    global _session, _session_at
    if _session is None or time.time() - _session_at > 1800:
        s = requests.Session()
        s.headers.update(_HEADERS)
        try:
            s.get(MIS_HOME, timeout=(4, 8))          # 取 session cookie（MIS 有時需要）
        except Exception:
            pass
        _session, _session_at = s, time.time()
    return _session


def _ex_ch(code: str, market: Optional[str]) -> List[str]:
    m = (market or "").upper()
    if m in ("TSE", "TWSE", "TW", "上市"):
        return [f"tse_{code}.tw"]
    if m in ("OTC", "TPEX", "TWO", "上櫃"):
        return [f"otc_{code}.tw"]
    return [f"tse_{code}.tw", f"otc_{code}.tw"]     # 不知道市場就兩邊都問，MIS 只回存在的那邊


def parse_mis_row(r: Dict) -> Optional[Dict]:
    """把 MIS 一筆 msgArray 轉成統一格式。沒有成交價（尚未開盤／暫無成交）→ price=None，不猜。"""
    code = r.get("c")
    if not code:
        return None
    # z 是「這一盤（約 5 秒）的成交價」，這一盤沒有成交時是 "-"；最近一筆成交在 trade.z（含成交時間 trade.t）。
    tr = r.get("trade") if isinstance(r.get("trade"), dict) else {}
    price = _num(r.get("z"))
    trade_time = r.get("t")
    if price is None:
        price = _num(tr.get("z"))
        if price is not None and tr.get("t"):
            trade_time = tr.get("t")
    prev = _num(r.get("y"))
    d = str(r.get("d") or "")
    date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None
    q = {"code": code, "name": r.get("n"), "market": (r.get("ex") or "").upper(),
         "price": price, "prev_close": prev,
         "open": _num(r.get("o")), "high": _num(r.get("h")), "low": _num(r.get("l")),
         "volume_lots": int(float(r["v"])) if _num(r.get("v")) else None,
         "limit_up": _num(r.get("u")), "limit_down": _num(r.get("w")),
         "time": trade_time, "date": date, "source": "證交所 MIS"}
    if price and prev:
        q["change"] = round(price - prev, 2)
        q["change_pct"] = round((price / prev - 1) * 100, 2)
    else:
        q["change"] = q["change_pct"] = None
    return q


def fetch_mis(pairs: Iterable[tuple]) -> Dict[str, Dict]:
    """pairs: [(code, market)]。回傳 {code: quote}。失敗（含被擋）回空 dict 並記錄原因。"""
    global _session
    out: Dict[str, Dict] = {}
    chans: List[str] = []
    for code, mk in pairs:
        chans += _ex_ch(code, mk)
    for i in range(0, len(chans), BATCH):
        part = chans[i:i + BATCH]
        try:
            r = _get_session().get(MIS_URL, params={"ex_ch": "|".join(part), "json": "1", "delay": "0",
                                                    "_": int(time.time() * 1000)}, timeout=(4, 10))
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            j = r.json()
            for row in j.get("msgArray") or []:
                q = parse_mis_row(row)
                if q:
                    out[q["code"]] = q
            _stats["mis_ok"] += 1
            _stats["last_ok_at"] = time.time()
        except Exception as e:
            _stats["mis_fail"] += 1
            _stats["last_error"] = f"MIS: {e}"
            logger.warning(f"realtime.fetch_mis 失敗: {e}")
            _session = None                              # 下次重建 session
        if i + BATCH < len(chans):
            time.sleep(0.4)
    return out


def fetch_fugle(code: str) -> Optional[Dict]:
    key = os.getenv("FUGLE_API_KEY", "")
    if not key:
        return None
    try:
        r = requests.get(f"{FUGLE_BASE}/stock/intraday/quote/{code}", headers={"X-API-KEY": key}, timeout=(3, 6))
        if r.status_code != 200:
            return None
        j = r.json()
        price = _num(j.get("lastPrice")) or _num(j.get("closePrice"))
        if not price:
            return None
        prev = _num(j.get("previousClose")) or _num(j.get("referencePrice"))
        d = j.get("date")
        tm = None
        if j.get("lastUpdated"):
            try:
                tm = datetime.fromtimestamp(int(j["lastUpdated"]) / 1e6, TZ).strftime("%H:%M:%S")
            except Exception:
                tm = None
        _stats["fugle_ok"] += 1
        return {"code": code, "name": j.get("name"), "market": None, "price": price, "prev_close": prev,
                "open": _num(j.get("openPrice")), "high": _num(j.get("highPrice")), "low": _num(j.get("lowPrice")),
                "volume_lots": (j.get("total") or {}).get("tradeVolume"),
                "time": tm, "date": d, "source": "富果 Fugle（備援）",
                "change": round(price - prev, 2) if prev else None,
                "change_pct": round((price / prev - 1) * 100, 2) if prev else None}
    except Exception as e:
        _stats["last_error"] = f"Fugle: {e}"
        return None


def get_quote(code: str, market: Optional[str] = None) -> Optional[Dict]:
    """單一檔即時報價（10 秒快取）。回傳含 live 欄位：資料日期是今天、且目前在盤中。"""
    code = code.split(".")[0].upper()
    with _lock:
        hit = _cache.get(code)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    q = fetch_mis([(code, market)]).get(code)
    if not q or not q.get("price"):
        fq = fetch_fugle(code)
        if fq:
            q = fq
    if not q:
        return None
    today = now_tpe().strftime("%Y-%m-%d")
    q["live"] = bool(q.get("date") == today and market_open())
    q["fetched_at"] = time.time()
    with _lock:
        _cache[code] = (time.time(), q)
        if len(_cache) > 800:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:200]:
                _cache.pop(k, None)
    return q


def compute_breadth(quotes: Dict[str, Dict], today: Optional[str] = None) -> Dict:
    """由即時報價算盤中市場廣度。只算資料日期是今天的；沒成交價的歸 no_trade，資料日期不是今天的歸 stale。"""
    today = today or now_tpe().strftime("%Y-%m-%d")
    b = {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "no_trade": 0, "stale": 0}
    for q in quotes.values():
        if q.get("date") != today:
            b["stale"] += 1
            continue
        p, pv = q.get("price"), q.get("prev_close")
        if not p or not pv:
            b["no_trade"] += 1
            continue
        b["up" if p > pv else "down" if p < pv else "flat"] += 1
        if q.get("limit_up") and p >= q["limit_up"]:
            b["limit_up"] += 1
        elif q.get("limit_down") and p <= q["limit_down"]:
            b["limit_down"] += 1
    return b


def fetch_indices() -> Dict[str, Dict]:
    """加權指數與櫃買指數（MIS：t00 / o00）。"""
    out = {}
    for key, ex, nm in (("TAIEX", "tse_t00.tw", "加權指數"), ("OTC", "otc_o00.tw", "櫃買指數")):
        try:
            r = _get_session().get(MIS_URL, params={"ex_ch": ex, "json": "1", "delay": "0",
                                                    "_": int(time.time() * 1000)}, timeout=(4, 8))
            rows = (r.json().get("msgArray") or []) if r.status_code == 200 else []
            q = parse_mis_row(rows[0]) if rows else None
            if q and q.get("price"):
                out[key] = {"name": nm, "price": q["price"], "prev_close": q["prev_close"], "change": q["change"],
                            "change_pct": q["change_pct"], "high": q["high"], "low": q["low"],
                            "time": q["time"], "date": q["date"]}
        except Exception as e:
            _stats["last_error"] = f"指數: {e}"
    return out


def snapshot(universe: List[Dict]) -> Dict:
    """對掃描池全部代號抓一次 MIS，回傳可存資料庫的快照（含廣度、指數、完整度）。"""
    pairs = [(s["code"], s.get("market")) for s in universe if s.get("code")]
    t0 = time.time()
    quotes = fetch_mis(pairs)
    slim = {c: [q["price"], q["prev_close"], q["volume_lots"], q["time"], q["date"],
                q.get("limit_up"), q.get("limit_down")] for c, q in quotes.items()}
    breadth = compute_breadth(quotes)
    missing = len(pairs) - len(quotes)          # 來源根本沒回（不同於「有回但沒成交」）
    return {"at": time.time(), "n_req": len(pairs), "n_ok": len(quotes), "n_missing": missing,
            "secs": round(time.time() - t0, 1), "quotes": slim, "breadth": breadth,
            "indices": fetch_indices(),
            "completeness": round(len(quotes) / len(pairs), 4) if pairs else None}


def fresh_prices(pairs: Iterable[tuple]) -> Dict:
    """風控用現價：只收「資料日期是今天」的即時成交價（MIS，抓不到再用 Fugle）。
    不退回 Yahoo／日線——現價不新鮮就不判斷，寧可漏報也不用舊價誤報。
    回傳 {"prices": {code: price}, "reasons": {code: 原因}}，原因為 no_trade / stale / source_missing。"""
    pairs = [(c.split(".")[0].upper(), m) for c, m in pairs]
    today = now_tpe().strftime("%Y-%m-%d")
    got = fetch_mis(pairs)
    prices, reasons = {}, {}
    for code, mk in pairs:
        q = got.get(code)
        if not q or not q.get("price"):
            fq = fetch_fugle(code)
            if fq and fq.get("date") in (today, None) and fq.get("price"):
                q = fq
        if not q:
            reasons[code] = "source_missing"
        elif not q.get("price"):
            reasons[code] = "no_trade"
        elif q.get("date") != today:
            reasons[code] = "stale"
        else:
            prices[code] = q["price"]
    return {"prices": prices, "reasons": reasons}


def status() -> Dict:
    return dict(_stats)


def probe(code: str, market: Optional[str] = None) -> Dict:
    """診斷用：原樣回傳 MIS 對單一代號的回應（含 HTTP 狀態、rtcode、原始欄位），看是哪一步出問題。"""
    try:
        r = _get_session().get(MIS_URL, params={"ex_ch": "|".join(_ex_ch(code, market)), "json": "1", "delay": "0",
                                                "_": int(time.time() * 1000)}, timeout=(4, 10))
        out = {"http": r.status_code, "len": len(r.text)}
        try:
            j = r.json()
            out["rtcode"] = j.get("rtcode")
            out["rtmessage"] = j.get("rtmessage")
            out["rows"] = (j.get("msgArray") or [])[:2]
        except Exception as e:
            out["body_head"] = r.text[:300]
            out["json_error"] = str(e)
        return out
    except Exception as e:
        return {"error": str(e)}
