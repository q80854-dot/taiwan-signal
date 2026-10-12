"""FinMind 資料模組（https://api.finmindtrade.com/api/v4/data）。

2026-10-12 用瀏覽器實測：未登入的免費額度可用的資料集有
  股價、本益比/淨值比/殖利率、融資融券、三大法人買賣超、外資持股、借券成交、當沖、
  月營收、綜合損益表、資產負債表、現金流量表、股利政策、個股新聞、
  大盤三大法人、大盤融資融券、台指期貨日成交、期貨三大法人。
需要付費等級（本模組不使用）：股權分散表、市值、大額交易人、券商分點、分 K。

設計原則——「準確、穩定」：
  1. 額度保護：未帶 token 每小時 300 次、帶 token 600 次。本模組自己記帳，用到 90% 就停止對外請求，
     改回傳快取（標記 stale），不讓一般使用者的查詢把額度耗光、拖累排程更新。
  2. 失敗不空白：任何請求失敗（逾時、額度、維護）都沿用上一次成功的資料，並標記 stale 與原因，
     前端會顯示「沿用前次資料」，不會假裝是最新。
  3. 每筆資料都帶資料日期；日期早於預期交易日就標記落後。
  4. token 只從環境變數 FINMIND_TOKEN 讀，以 Authorization 標頭送出，不寫進日誌、不回傳給前端。
"""
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
HOURLY_LIMIT = 600 if TOKEN else 300
BUDGET = int(HOURLY_LIMIT * 0.9)            # 留 10% 給排程與意外
_calls: deque = deque()                     # 最近一小時每次對外請求的時間
_lock = threading.Lock()
_cache: Dict[tuple, dict] = {}
_auth_mode = "header"                       # 目前有效的 token 傳法（header／param）
_token_bad_until = 0.0                      # token 被拒時，暫時改用免登入額度到這個時間
_blocked_until = 0.0                        # 被 FinMind 回報額度用完／維護時，暫停到這個時間
_stats = {"calls": 0, "cache_hits": 0, "errors": 0, "last_error": None, "last_ok": None, "last_http": None, "auth": None}
_POOL = ThreadPoolExecutor(max_workers=5)
_MAX_CACHE = 800
_FIN_DAYS = 1500                              # 三張財報共用同一個查詢區間（約 16 季），才會命中同一份快取、不多吃額度

# 各資料的快取秒數：盤後資料一天才變一次，不必頻繁問；新聞與當日籌碼短一點。
TTL = {
    "TaiwanStockPrice": 900, "TaiwanStockPER": 3600, "TaiwanStockDayTrading": 1800,
    "TaiwanStockMarginPurchaseShortSale": 1800, "TaiwanStockInstitutionalInvestorsBuySell": 1800,
    "TaiwanStockShareholding": 3600, "TaiwanStockSecuritiesLending": 3600,
    "TaiwanStockMonthRevenue": 6 * 3600, "TaiwanStockFinancialStatements": 12 * 3600,
    "TaiwanStockBalanceSheet": 12 * 3600, "TaiwanStockCashFlowsStatement": 12 * 3600,
    "TaiwanStockDividend": 24 * 3600, "TaiwanStockNews": 900,
    "TaiwanStockTotalInstitutionalInvestors": 900, "TaiwanStockTotalMarginPurchaseShortSale": 900,
    "TaiwanFuturesDaily": 600, "TaiwanFuturesInstitutionalInvestors": 600,
}


# ───────── 持久快取（寫進 DB）─────────
# 部署／重啟會清空記憶體；若 FinMind 當下又剛好額度用完（402），整頁資料就會空白。
# 所以每次成功取得的資料都存進 DB（壓縮後），重啟後直接沿用，額度暫停期間也能顯示最近一次成功的資料。
_PERSIST_MAX = 700_000          # 壓縮後超過這個大小就不存
_boot_loaded = False


def _pkey(key: tuple) -> str:
    return "fm:" + hashlib.sha1(repr(key).encode()).hexdigest()[:28]


def _pget(name: str):
    try:
        from state_store import store
        return store.get_meta(name, None)
    except Exception:
        return None


def _pset(name: str, value: str):
    try:
        from state_store import store
        store.set_meta(name, value)
    except Exception as e:
        logger.debug("finmind persist %s: %s", name, e)


def _persist_load(key: tuple) -> Optional[dict]:
    raw = _pget(_pkey(key))
    if not raw or not isinstance(raw, str):
        return None
    try:
        d = json.loads(zlib.decompress(base64.b64decode(raw)).decode("utf-8"))
        return {"rows": d["rows"], "ts": float(d["ts"])}
    except Exception:
        return None


def _persist_save(key: tuple, rows: list, ts: float):
    try:
        raw = base64.b64encode(zlib.compress(json.dumps({"ts": ts, "rows": rows}, ensure_ascii=False,
                                                        separators=(",", ":")).encode("utf-8"), 6)).decode("ascii")
        if len(raw) <= _PERSIST_MAX:
            _pset(_pkey(key), raw)
    except Exception as e:
        logger.debug("finmind persist save: %s", e)


def _load_block_once():
    """重啟後沿用上次的『暫停到何時』，避免剛重啟就又去撞已經用完的額度。"""
    global _boot_loaded, _blocked_until
    if _boot_loaded:
        return
    _boot_loaded = True
    try:
        v = float(_pget("fm:blocked_until") or 0)
        if v > time.time():
            _blocked_until = max(_blocked_until, v)
    except Exception:
        pass


def tpe_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _day(offset_days: int = 0) -> str:
    return (tpe_now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def budget_left() -> int:
    now = time.time()
    with _lock:
        while _calls and now - _calls[0] > 3600:
            _calls.popleft()
        return max(0, BUDGET - len(_calls))


def status() -> Dict:
    """給管理頁／健康檢查：額度、快取、最近錯誤（不含 token）。"""
    return {"has_token": bool(TOKEN), "hourly_limit": HOURLY_LIMIT, "budget_left": budget_left(),
            "cache_entries": len(_cache), "blocked_for_sec": max(0, int(_blocked_until - time.time())),
            "token_fallback_sec": max(0, int(_token_bad_until - time.time())), **_stats}


def _prune():
    if len(_cache) <= _MAX_CACHE:
        return
    for k in sorted(_cache, key=lambda x: _cache[x]["ts"])[: len(_cache) - _MAX_CACHE + 50]:
        _cache.pop(k, None)


def fm_get(dataset: str, data_id: Optional[str] = None, start: Optional[str] = None,
           end: Optional[str] = None, ttl: Optional[int] = None, force: bool = False) -> Dict:
    """→ {rows, ok, stale, fetched_at, error}。永遠回傳 dict，不丟例外。"""
    global _blocked_until, _token_bad_until, _auth_mode
    key = (dataset, data_id, start, end)
    ttl = TTL.get(dataset, 900) if ttl is None else ttl
    _load_block_once()
    c = _cache.get(key)
    if c is None:
        c = _persist_load(key)
        if c:
            _cache[key] = c
    if c and not force and time.time() - c["ts"] < ttl:
        _stats["cache_hits"] += 1
        return {"rows": c["rows"], "ok": True, "stale": False, "fetched_at": c["ts"], "error": None}

    def fallback(err: str) -> Dict:
        _stats["errors"] += 1
        _stats["last_error"] = f"{dataset}: {err}"[:200]
        if c:
            return {"rows": c["rows"], "ok": True, "stale": True, "fetched_at": c["ts"], "error": err}
        return {"rows": [], "ok": False, "stale": False, "fetched_at": None, "error": err}

    if time.time() < _blocked_until:
        return fallback("FinMind 暫停請求中（額度或維護）")
    if budget_left() <= 0:
        return fallback("本小時 FinMind 額度已用到保護線，改用快取")
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start:
        params["start_date"] = start
    if end:
        params["end_date"] = end
    last = "未知錯誤"
    # 認證順序：token 放標頭 → token 放參數（兩種 FinMind 都支援，記住哪種有效）→ 免登入。
    # 帶 token 全被拒時 10 分鐘內直接走免登入，不讓單一 token 問題讓整站沒資料。
    modes = []
    if TOKEN and time.time() >= _token_bad_until:
        modes = ["param", "header"] if _auth_mode == "param" else ["header", "param"]
    modes.append("anon")
    token_denied = 0
    for mode in modes:
        headers = {"Authorization": f"Bearer {TOKEN}"} if mode == "header" else {}
        prm = dict(params, token=TOKEN) if mode == "param" else params
        try:
            with _lock:
                _calls.append(time.time())
            _stats["calls"] += 1
            r = requests.get(BASE, params=prm, headers=headers, timeout=20)
            try:
                j = r.json()
            except Exception:
                j = {}
            st = j.get("status", r.status_code)
            msg = str(j.get("msg", ""))[:80]
            if st == 200 and r.status_code == 200:
                rows = j.get("data") or []
                _cache[key] = {"rows": rows, "ts": time.time()}
                _persist_save(key, rows, _cache[key]["ts"])
                _prune()
                _stats["last_ok"] = time.time()
                _stats["last_http"] = None
                _stats["auth"] = mode
                if mode != "anon":
                    _auth_mode = mode
                elif token_denied:
                    _token_bad_until = time.time() + 600     # 帶 token 都被拒、免登入可用：10 分鐘內直接走免登入
                return {"rows": rows, "ok": True, "stale": False, "fetched_at": _cache[key]["ts"], "error": None}
            _stats["last_http"] = f"{dataset} {mode} HTTP {r.status_code} status {st} {msg}"[:200]
            denied = r.status_code in (401, 402, 403, 429) or st in (401, 402, 403, 429) or "limit" in msg.lower() or "token" in msg.lower()
            if denied and mode != "anon":
                token_denied += 1
                continue
            if denied:
                if token_denied:
                    _token_bad_until = time.time() + 600
                _blocked_until = time.time() + 300      # 帶 token 與免登入都被拒＝額度真的用完，暫停 5 分鐘（寫進 DB，重啟也記得）
                _pset("fm:blocked_until", str(_blocked_until))
                return fallback(f"HTTP {r.status_code} status {st} {msg}")
            return fallback(f"status {st} {msg}")
        except Exception as e:   # 逾時、連線中斷、非 JSON（只記例外類型，避免把含 token 的網址寫進日誌）
            last = f"{type(e).__name__}"
            time.sleep(1.0)
    return fallback(last)


# ───────── 小工具 ─────────
def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v == v else None
    except Exception:
        return None


def _safe_url(u) -> str:
    u = str(u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else ""


def _lots(shares) -> int:
    return int(round((_f(shares) or 0) / 1000))


def _sec(res: Dict, rows=None, extra: Optional[Dict] = None) -> Dict:
    d = {"ok": bool(res.get("ok")) and bool(res["rows"] if rows is None else rows),
         "stale": bool(res.get("stale")), "error": res.get("error")}
    if extra:
        d.update(extra)
    return d


def _clean_code(code: str) -> str:
    code = str(code or "").split(".")[0].strip().upper()
    return code if re.match(r"^[0-9A-Z]{3,8}$", code) else ""


# ───────── 個股：K 線補最新 ─────────
def price_bars(code: str, days: int = 20) -> Tuple[List[Dict], Dict]:
    """FinMind 日 K → [{date,open,high,low,close,volume(張)}]。volume 為 0 的（尚未成交／休市占位）略過。"""
    res = fm_get("TaiwanStockPrice", code, _day(-days))
    bars = []
    for r in res["rows"]:
        o, h, l, c, v = (_f(r.get(k)) for k in ("open", "max", "min", "close", "Trading_Volume"))
        if None in (o, h, l, c, v) or c <= 0 or v <= 0 or h < l:
            continue
        bars.append({"date": str(r["date"])[:10], "open": round(o, 2), "high": round(h, 2),
                     "low": round(l, 2), "close": round(c, 2), "volume": int(v // 1000)})
    bars.sort(key=lambda b: b["date"])
    return bars, res


def newer_bars(code: str, cached_last_date: Optional[str]) -> List[Dict]:
    """快取最後一根之後的新 K 棒（K 線沿用舊資料時用來補上）。"""
    try:
        bars, _ = price_bars(code, 12)
    except Exception as e:
        logger.warning("finmind newer_bars %s: %s", code, e)
        return []
    ld = (cached_last_date or "")[:10]
    return [b for b in bars if b["date"] > ld]


def append_to_series(d: Dict, code: str) -> int:
    """d 是 {dates,opens,highs,lows,closes,volumes} 的欄位式 K 線；把 FinMind 比最後一根更新的日 K 接在後面。回傳新增根數。"""
    if not d or not d.get("dates"):
        return 0
    new = newer_bars(code, d["dates"][-1])
    for b in new:
        d["dates"].append(b["date"])
        d["opens"].append(b["open"]); d["highs"].append(b["high"]); d["lows"].append(b["low"])
        d["closes"].append(b["close"]); d["volumes"].append(b["volume"])
    return len(new)


# ───────── 個股研究 ─────────
def _by_date(rows: List[Dict]) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for r in rows:
        out.setdefault(str(r.get("date"))[:10], []).append(r)
    return out


def sec_chips(code: str) -> Dict:
    """三大法人買賣超（張）：近 10 日、5 日／20 日累計、連買連賣天數。"""
    res = fm_get("TaiwanStockInstitutionalInvestorsBuySell", code, _day(-45))
    days = []
    for d, rs in sorted(_by_date(res["rows"]).items()):
        v = {"foreign": 0, "trust": 0, "dealer": 0}
        for r in rs:
            net = _lots((_f(r.get("buy")) or 0) - (_f(r.get("sell")) or 0))
            n = r.get("name")
            if n in ("Foreign_Investor", "Foreign_Dealer_Self"):
                v["foreign"] += net
            elif n == "Investment_Trust":
                v["trust"] += net
            elif n in ("Dealer_self", "Dealer_Hedging"):
                v["dealer"] += net
        days.append({"date": d, **v, "total": v["foreign"] + v["trust"] + v["dealer"]})
    out = _sec(res, days)
    if days:
        def streak(k):
            n, s = 0, 0
            for x in reversed(days):
                if x[k] > 0 and s >= 0:
                    s, n = 1, n + 1
                elif x[k] < 0 and s <= 0:
                    s, n = -1, n - 1
                else:
                    break
            return n
        out.update({
            "date": days[-1]["date"], "days": days[-10:],
            "sum5": {k: sum(x[k] for x in days[-5:]) for k in ("foreign", "trust", "dealer", "total")},
            "sum20": {k: sum(x[k] for x in days[-20:]) for k in ("foreign", "trust", "dealer", "total")},
            "streak": {"foreign": streak("foreign"), "trust": streak("trust"), "dealer": streak("dealer")},
        })
    return out


def sec_margin(code: str) -> Dict:
    res = fm_get("TaiwanStockMarginPurchaseShortSale", code, _day(-30))
    days = []
    for r in sorted(res["rows"], key=lambda x: x.get("date", "")):
        mb, sb = _f(r.get("MarginPurchaseTodayBalance")), _f(r.get("ShortSaleTodayBalance"))
        if mb is None or sb is None:
            continue
        mp, sp = _f(r.get("MarginPurchaseYesterdayBalance")), _f(r.get("ShortSaleYesterdayBalance"))
        days.append({"date": str(r["date"])[:10], "margin": int(mb), "short": int(sb),
                     "margin_chg": int(mb - mp) if mp is not None else None,
                     "short_chg": int(sb - sp) if sp is not None else None,
                     "ratio": round(sb / mb * 100, 1) if mb else None,
                     "margin_limit": int(_f(r.get("MarginPurchaseLimit")) or 0) or None})
    out = _sec(res, days)
    if days:
        last = days[-1]
        out.update({"date": last["date"], "days": days[-8:], "latest": last})
        if last["margin_limit"]:
            out["margin_use_pct"] = round(last["margin"] / last["margin_limit"] * 100, 1)
    return out


def sec_lending(code: str) -> Dict:
    res = fm_get("TaiwanStockSecuritiesLending", code, _day(-14))
    days = []
    for d, rs in sorted(_by_date(res["rows"]).items()):
        vol = sum(_f(r.get("volume")) or 0 for r in rs)
        days.append({"date": d, "lots": _lots(vol), "n": len(rs)})
    out = _sec(res, days)
    if days:
        out.update({"date": days[-1]["date"], "days": days[-5:]})
    return out


def sec_foreign_hold(code: str) -> Dict:
    res = fm_get("TaiwanStockShareholding", code, _day(-40))
    days = []
    for r in sorted(res["rows"], key=lambda x: x.get("date", "")):
        ratio = _f(r.get("ForeignInvestmentSharesRatio"))
        if ratio is not None:
            days.append({"date": str(r["date"])[:10], "ratio": round(ratio, 2),
                         "limit": _f(r.get("ForeignInvestmentUpperLimitRatio"))})
    out = _sec(res, days)
    if days:
        out.update({"date": days[-1]["date"], "latest": days[-1], "days": days[-20:]})
        if len(days) >= 6:
            out["chg5"] = round(days[-1]["ratio"] - days[-6]["ratio"], 2)
        if len(days) >= 2:
            out["chg_all"] = round(days[-1]["ratio"] - days[0]["ratio"], 2)
    return out


def sec_valuation(code: str) -> Dict:
    """本益比／淨值比／殖利率 與 近一年相對位置。"""
    res = fm_get("TaiwanStockPER", code, _day(-400))
    rows = [r for r in res["rows"] if _f(r.get("PER")) is not None]
    out = _sec(res, rows)
    if not rows:
        return out
    rows.sort(key=lambda x: x["date"])
    last = rows[-1]

    def pct(key):
        vals = [_f(r.get(key)) for r in rows if _f(r.get(key)) and _f(r.get(key)) > 0]
        cur = _f(last.get(key))
        if len(vals) < 30 or not cur or cur <= 0:
            return None
        return {"cur": round(cur, 2), "min": round(min(vals), 2), "max": round(max(vals), 2),
                "median": round(sorted(vals)[len(vals) // 2], 2),
                "pctile": round(sum(1 for v in vals if v <= cur) / len(vals) * 100)}
    out.update({"date": last["date"], "per": pct("PER"), "pbr": pct("PBR"),
                "yield": _f(last.get("dividend_yield")), "n_days": len(rows)})
    return out


def sec_daytrade(code: str) -> Dict:
    res = fm_get("TaiwanStockDayTrading", code, _day(-14))
    pr = {b["date"]: b for b in price_bars(code, 14)[0]}
    days = []
    for r in sorted(res["rows"], key=lambda x: x.get("date", "")):
        v = _f(r.get("Volume"))
        d = str(r["date"])[:10]
        if not v or v <= 0 or d not in pr:
            continue
        tot = pr[d]["volume"] * 1000
        days.append({"date": d, "lots": _lots(v), "ratio": round(v / tot * 100, 1) if tot else None})
    out = _sec(res, days)
    if days:
        out.update({"date": days[-1]["date"], "days": days[-5:]})
    return out


def sec_revenue(code: str) -> Dict:
    res = fm_get("TaiwanStockMonthRevenue", code, _day(-820))
    by = {}
    for r in res["rows"]:
        y, m, rev = r.get("revenue_year"), r.get("revenue_month"), _f(r.get("revenue"))
        if y and m and rev is not None:
            by[(int(y), int(m))] = rev
    months = []
    for (y, m) in sorted(by):
        cur = by[(y, m)]
        ly = by.get((y - 1, m))
        pm = by.get((y, m - 1) if m > 1 else (y - 1, 12))
        months.append({"ym": f"{y}-{m:02d}", "revenue": cur,
                       "yoy": round((cur / ly - 1) * 100, 1) if ly else None,
                       "mom": round((cur / pm - 1) * 100, 1) if pm else None})
    out = _sec(res, months)
    if months:
        ly_, lm = months[-1]["ym"].split("-")
        ytd = sum(v for (y, m), v in by.items() if y == int(ly_) and m <= int(lm))
        ytd_ly = sum(v for (y, m), v in by.items() if y == int(ly_) - 1 and m <= int(lm))
        out.update({"latest": months[-1], "months": months[-13:],
                    "ytd_yoy": round((ytd / ytd_ly - 1) * 100, 1) if ytd_ly else None})
        yo = [m["yoy"] for m in months[-3:] if m["yoy"] is not None]
        out["yoy_3m_avg"] = round(sum(yo) / len(yo), 1) if yo else None
    return out


def sec_income(code: str) -> Dict:
    res = fm_get("TaiwanStockFinancialStatements", code, _day(-_FIN_DAYS))
    q = {}
    for r in res["rows"]:
        v = _f(r.get("value"))
        if v is not None:
            q.setdefault(str(r["date"])[:10], {})[r.get("type")] = v
    rows = []
    dates = sorted(q)
    for d in dates:
        x = q[d]
        rev = x.get("Revenue")
        if not rev:
            continue
        rows.append({"date": d, "revenue": rev, "gross": x.get("GrossProfit"),
                     "op": x.get("OperatingIncome"), "net": x.get("IncomeAfterTaxes"), "eps": x.get("EPS"),
                     "gm": round(x["GrossProfit"] / rev * 100, 1) if x.get("GrossProfit") is not None else None,
                     "om": round(x["OperatingIncome"] / rev * 100, 1) if x.get("OperatingIncome") is not None else None,
                     "nm": round(x["IncomeAfterTaxes"] / rev * 100, 1) if x.get("IncomeAfterTaxes") is not None else None})
    for i, r in enumerate(rows):
        ly = next((p for p in rows[:i] if p["date"][5:] == r["date"][5:] and int(r["date"][:4]) - int(p["date"][:4]) == 1), None)
        r["rev_yoy"] = round((r["revenue"] / ly["revenue"] - 1) * 100, 1) if ly and ly["revenue"] else None
        r["eps_yoy"] = round((r["eps"] / ly["eps"] - 1) * 100, 1) if ly and ly["eps"] and r["eps"] is not None and ly["eps"] > 0 else None
    out = _sec(res, rows)
    if rows:
        eps4 = [r["eps"] for r in rows[-4:] if r["eps"] is not None]
        out.update({"date": rows[-1]["date"], "quarters": rows[-8:], "latest": rows[-1],
                    "eps_ttm": round(sum(eps4), 2) if len(eps4) == 4 else None})
    return out


def _pick(d: Dict[str, float], *names, contains: str = "") -> Optional[float]:
    for n in names:
        if d.get(n) is not None:
            return d[n]
    if contains:
        for k, v in d.items():
            if contains in k and not k.endswith("_per"):
                return v
    return None


def sec_balance(code: str) -> Dict:
    res = fm_get("TaiwanStockBalanceSheet", code, _day(-_FIN_DAYS))
    q = {}
    for r in res["rows"]:
        v = _f(r.get("value"))
        if v is not None:
            q.setdefault(str(r["date"])[:10], {})[r.get("type")] = v
    rows = []
    for d in sorted(q):
        x = q[d]
        ta, tl = _pick(x, "TotalAssets"), _pick(x, "Liabilities", "TotalLiabilities")
        ca, cl = _pick(x, "CurrentAssets"), _pick(x, "CurrentLiabilities")
        if tl is None and cl is not None and x.get("NoncurrentLiabilities") is not None:
            tl = cl + x["NoncurrentLiabilities"]
        eq, inv = _pick(x, "Equity", "TotalEquity"), _pick(x, "Inventories")
        if eq is None and ta and tl is not None:
            eq = ta - tl
        cash = _pick(x, "CashAndCashEquivalents")
        if not ta:
            continue
        rows.append({"date": d, "assets": ta, "liabilities": tl, "equity": eq, "cash": cash,
                     "debt_ratio": round(tl / ta * 100, 1) if tl is not None else None,
                     "current_ratio": round(ca / cl * 100, 1) if ca and cl else None,
                     "quick_ratio": round((ca - (inv or 0)) / cl * 100, 1) if ca and cl else None})
    out = _sec(res, rows)
    if rows:
        out.update({"date": rows[-1]["date"], "latest": rows[-1], "quarters": rows[-5:]})
    return out


def sec_cashflow(code: str) -> Dict:
    """FinMind 現金流量表是「當年度累計」（Q2 = 上半年、Q4 = 全年），要把同年度前一季相減才是單季。
    2026-10-12 以台積電實測：2025Q2 營業現金流 1.12 兆、Q3 1.55 兆、Q4 2.27 兆、2026Q1 又從 0.70 兆重新累計。"""
    res = fm_get("TaiwanStockCashFlowsStatement", code, _day(-_FIN_DAYS))
    q = {}
    for r in res["rows"]:
        v = _f(r.get("value"))
        if v is not None:
            q.setdefault(str(r["date"])[:10], {})[r.get("type")] = v
    cum = []
    for d in sorted(q):
        x = q[d]
        ocf = _pick(x, "CashFlowsFromOperatingActivities", "NetCashInflowFromOperatingActivities", contains="OperatingActivities")
        if ocf is None:
            continue
        cum.append({"date": d, "ocf": ocf,
                    "icf": _pick(x, "CashProvidedByInvestingActivities", contains="InvestingActivities"),
                    "fin": _pick(x, "CashFlowsProvidedFromFinancingActivities", "CashFlowsFromFinancingActivities", contains="FinancingActivities"),
                    "capex": _pick(x, "PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment", contains="AcquisitionOfPropertyPlantAndEquipment")})
    rows = []
    for i, c in enumerate(cum):
        prev = cum[i - 1] if i and cum[i - 1]["date"][:4] == c["date"][:4] else None
        if c["date"][5:7] != "03" and not prev:
            continue                       # 缺同年度前一季，無法還原單季，寧可不顯示
        def one(k):
            if c.get(k) is None:
                return None
            return c[k] - (prev[k] if prev and prev.get(k) is not None else 0)
        ocf, capex = one("ocf"), one("capex")
        rows.append({"date": c["date"], "ocf": ocf, "icf": one("icf"), "fin": one("fin"), "capex": capex,
                     "fcf": (ocf - abs(capex)) if capex is not None and ocf is not None else None,
                     "ocf_ytd": c["ocf"]})
    out = _sec(res, rows)
    if rows:
        out.update({"date": rows[-1]["date"], "latest": rows[-1], "quarters": rows[-5:]})
    return out


def sec_dividend(code: str) -> Dict:
    res = fm_get("TaiwanStockDividend", code, _day(-365 * 6))
    rows = []
    for r in sorted(res["rows"], key=lambda x: x.get("date", "")):
        cash = (_f(r.get("CashEarningsDistribution")) or 0) + (_f(r.get("CashStatutorySurplus")) or 0)
        stock = (_f(r.get("StockEarningsDistribution")) or 0) + (_f(r.get("StockStatutorySurplus")) or 0)
        rows.append({"period": str(r.get("year", "")), "date": str(r.get("date", ""))[:10], "cash": round(cash, 4),
                     "stock": round(stock, 4), "ex_cash": str(r.get("CashExDividendTradingDate") or "")[:10] or None,
                     "ex_stock": str(r.get("StockExDividendTradingDate") or "")[:10] or None,
                     "pay": str(r.get("CashDividendPaymentDate") or "")[:10] or None})
    out = _sec(res, rows)
    if rows:
        out.update({"records": rows[-8:]})
    return out


def sec_news(code: str) -> Dict:
    res = fm_get("TaiwanStockNews", code, _day(-7))
    rows = []
    seen = set()
    for r in sorted(res["rows"], key=lambda x: str(x.get("date", "")), reverse=True):
        t = str(r.get("title") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        rows.append({"date": str(r.get("date", ""))[:16], "title": t[:120], "source": str(r.get("source") or "")[:20],
                     "link": _safe_url(r.get("link"))})
        if len(rows) >= 12:
            break
    return _sec(res, rows, {"items": rows})


def _qlabel(d: str) -> str:
    return f"{d[:4]}/Q{(int(d[5:7]) - 1) // 3 + 1}"


def _yoy_pct(cur, prev) -> Optional[float]:
    """同比增長率（相對變化 %）。去年同期為 0 或負數時沒有意義，回傳 None，不硬算。"""
    if cur is None or prev is None or prev <= 0:
        return None
    return round((cur / prev - 1) * 100, 2)


def sec_indicators(code: str) -> Dict:
    """財務指標（單季）。全部由 FinMind 三大報表原始科目直接計算，每個數字都能對回財報：
      每股盈餘 EPS             ＝ 損益表 EPS（單季）
      自由現金流               ＝ 營業活動現金流 − 購置不動產廠房及設備（現金流量表為年度累計，已相減還原單季）
      流動比率                 ＝ 流動資產 ÷ 流動負債
      股東權益比率             ＝ 權益總額 ÷ 資產總額
      淨資產收益率 ROE（單季） ＝ 歸屬母公司淨利 ÷ 歸屬母公司權益（期末）；缺母公司科目時改用稅後淨利 ÷ 權益總額
      總資產收益率 ROA（單季） ＝ 稅後淨利 ÷ 資產總額（期末）
      毛利率／營業利益率／淨利率 ＝ 毛利／營業利益／稅後淨利 ÷ 營業收入
    科目缺漏的季度該指標留空，不推估。同比＝與去年同季相比。"""
    inc_r = fm_get("TaiwanStockFinancialStatements", code, _day(-_FIN_DAYS))
    bs_r = fm_get("TaiwanStockBalanceSheet", code, _day(-_FIN_DAYS))
    cf_r = fm_get("TaiwanStockCashFlowsStatement", code, _day(-_FIN_DAYS))

    def pivot(res):
        q: Dict[str, Dict[str, float]] = {}
        for r in res["rows"]:
            v = _f(r.get("value"))
            if v is not None:
                q.setdefault(str(r["date"])[:10], {})[r.get("type")] = v
        return q
    inc, bs, cf = pivot(inc_r), pivot(bs_r), pivot(cf_r)

    def cfv(x):
        return (_pick(x, "CashFlowsFromOperatingActivities", "NetCashInflowFromOperatingActivities", contains="OperatingActivities"),
                _pick(x, "PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment", contains="AcquisitionOfPropertyPlantAndEquipment"))
    fcf: Dict[str, float] = {}
    cds = sorted(cf)
    for i, d in enumerate(cds):
        ocf, capex = cfv(cf[d])
        if ocf is None or capex is None:
            continue
        if d[5:7] == "03":
            fcf[d] = ocf - abs(capex)
            continue
        pd_ = cds[i - 1] if i and cds[i - 1][:4] == d[:4] else None
        if pd_ is None:
            continue                       # 缺同年度前一季，無法還原單季，寧可留空
        po, pc = cfv(cf[pd_])
        if po is None or pc is None:
            continue
        fcf[d] = (ocf - po) - (abs(capex) - abs(pc))
    vals: Dict[str, Dict[str, float]] = {}
    for d in sorted(set(inc) | set(bs) | set(fcf)):
        i_, b_ = inc.get(d, {}), bs.get(d, {})
        v: Dict[str, float] = {}
        rev, net = i_.get("Revenue"), i_.get("IncomeAfterTaxes")
        if i_.get("EPS") is not None:
            v["eps"] = i_["EPS"]
        if rev:
            if i_.get("GrossProfit") is not None:
                v["gross"] = i_["GrossProfit"] / rev * 100
            if i_.get("OperatingIncome") is not None:
                v["op"] = i_["OperatingIncome"] / rev * 100
            if net is not None:
                v["net"] = net / rev * 100
        ta, eq, peq = b_.get("TotalAssets"), b_.get("Equity"), b_.get("EquityAttributableToOwnersOfParent")
        pni = i_.get("EquityAttributableToOwnersOfParent")
        if pni is not None and peq:
            v["roe"] = pni / peq * 100
        elif net is not None and eq:
            v["roe"] = net / eq * 100
        if net is not None and ta:
            v["roa"] = net / ta * 100
        if b_.get("CurrentAssets") and b_.get("CurrentLiabilities"):
            v["current_ratio"] = b_["CurrentAssets"] / b_["CurrentLiabilities"] * 100
        if eq is not None and ta:
            v["equity_ratio"] = eq / ta * 100
        if d in fcf:
            v["fcf"] = fcf[d]
        vals[d] = v
    spec = [("eps", "每股收益", "元"), ("fcf", "自由現金流", "億"), ("current_ratio", "流動比率", "%"),
            ("equity_ratio", "股東權益比率", "%"), ("roe", "淨資產收益率", "%"), ("roa", "總資產收益率", "%"),
            ("gross", "毛利率", "%"), ("op", "營業利益率", "%"), ("net", "淨利率", "%")]
    metrics = []
    for key, label, unit in spec:
        rows = []
        for d in sorted(vals):
            if key not in vals[d]:
                continue
            ly = f"{int(d[:4]) - 1}{d[4:]}"
            cur, prev = vals[d][key], vals.get(ly, {}).get(key)
            val = cur / 1e8 if key == "fcf" else cur
            rows.append({"period": _qlabel(d), "date": d, "value": round(val, 2),
                         "yoy": _yoy_pct(cur, prev),
                         "yoy_pp": round(cur - prev, 2) if prev is not None and unit == "%" else None})
        if rows:
            metrics.append({"key": key, "label": label, "unit": unit, "rows": rows[-12:]})
    ok = bool(metrics)
    return {"ok": ok, "stale": bool(inc_r.get("stale") or bs_r.get("stale") or cf_r.get("stale")),
            "error": None if ok else (inc_r.get("error") or bs_r.get("error") or "無資料"),
            "date": max(vals) if vals else None, "metrics": metrics}


SECTIONS = {"chips": sec_chips, "margin": sec_margin, "lending": sec_lending, "foreign_hold": sec_foreign_hold,
            "valuation": sec_valuation, "daytrade": sec_daytrade, "revenue": sec_revenue, "income": sec_income,
            "balance": sec_balance, "cashflow": sec_cashflow, "dividend": sec_dividend, "news": sec_news}


def insights(p: Dict) -> List[Dict]:
    """把數字翻成白話（只描述，不下買賣建議）。level: good/warn/info。"""
    out = []

    def add(level, text):
        out.append({"level": level, "text": text})
    c = p.get("chips") or {}
    if c.get("ok"):
        s5, st = c["sum5"], c["streak"]
        add("good" if s5["total"] > 0 else "warn",
            f"三大法人近 5 日合計{'買超' if s5['total'] > 0 else '賣超'} {abs(s5['total']):,} 張"
            f"（外資 {s5['foreign']:+,}、投信 {s5['trust']:+,}、自營 {s5['dealer']:+,}）。")
        if st["foreign"] >= 3:
            add("good", f"外資連續買超 {st['foreign']} 天。")
        elif st["foreign"] <= -3:
            add("warn", f"外資連續賣超 {-st['foreign']} 天。")
        if st["trust"] >= 3:
            add("good", f"投信連續買超 {st['trust']} 天，投信偏好中長線，連買常代表有計畫布局。")
    m = p.get("margin") or {}
    if m.get("ok"):
        la = m["latest"]
        if la.get("ratio") is not None and la["ratio"] >= 15:
            add("warn", f"券資比 {la['ratio']}%，空方部位偏高，若股價轉強有軋空可能，反之表示市場看空意願強。")
        if la.get("margin_chg") is not None and m["days"] and len(m["days"]) >= 5:
            tot = sum(x["margin_chg"] or 0 for x in m["days"][-5:])
            if tot > 0 and (p.get("chips") or {}).get("sum5", {}).get("total", 0) < 0:
                add("warn", f"融資近 5 日增加 {tot:,} 張、但法人同期賣超：籌碼由散戶承接，較不穩。")
        if m.get("margin_use_pct") and m["margin_use_pct"] >= 60:
            add("warn", f"融資使用率 {m['margin_use_pct']}%，偏高，下跌時可能引發斷頭賣壓。")
    v = p.get("valuation") or {}
    if v.get("ok") and v.get("per"):
        pe = v["per"]
        pos = "偏高" if pe["pctile"] >= 80 else "偏低" if pe["pctile"] <= 20 else "中間區間"
        add("warn" if pe["pctile"] >= 80 else "good" if pe["pctile"] <= 20 else "info",
            f"本益比 {pe['cur']} 倍，落在近一年 {pe['pctile']}% 位置（{pos}；近一年 {pe['min']}～{pe['max']}）。")
    r = p.get("revenue") or {}
    if r.get("ok") and r.get("latest"):
        lt = r["latest"]
        if lt.get("yoy") is not None:
            add("good" if lt["yoy"] > 0 else "warn",
                f"{lt['ym']} 營收年增 {lt['yoy']:+.1f}%" + (f"、月增 {lt['mom']:+.1f}%" if lt.get("mom") is not None else "")
                + (f"；今年累計年增 {r['ytd_yoy']:+.1f}%" if r.get("ytd_yoy") is not None else "") + "。")
    i = p.get("income") or {}
    if i.get("ok") and i.get("latest"):
        lt = i["latest"]
        bits = [f"{lt['date'][:7]} 季毛利率 {lt['gm']}%" if lt.get("gm") is not None else "",
                f"營益率 {lt['om']}%" if lt.get("om") is not None else "",
                f"EPS {lt['eps']}" if lt.get("eps") is not None else ""]
        add("info", "、".join(b for b in bits if b) + (f"；近四季 EPS 合計 {i['eps_ttm']}" if i.get("eps_ttm") is not None else "") + "。")
        qs = i.get("quarters") or []
        if len(qs) >= 2 and qs[-1].get("gm") is not None and qs[-2].get("gm") is not None:
            d = qs[-1]["gm"] - qs[-2]["gm"]
            if abs(d) >= 1:
                add("good" if d > 0 else "warn", f"毛利率較上一季{'提高' if d > 0 else '下滑'} {abs(d):.1f} 個百分點。")
    b = p.get("balance") or {}
    if b.get("ok") and b["latest"].get("debt_ratio") is not None:
        lt = b["latest"]
        add("warn" if lt["debt_ratio"] >= 60 else "info",
            f"負債比 {lt['debt_ratio']}%" + (f"、流動比 {lt['current_ratio']}%" if lt.get("current_ratio") else "")
            + ("（短期償債能力偏弱）" if lt.get("current_ratio") and lt["current_ratio"] < 100 else "") + "。")
    cf = p.get("cashflow") or {}
    if cf.get("ok") and cf["latest"].get("ocf") is not None:
        add("good" if cf["latest"]["ocf"] > 0 else "warn",
            "最近一季營業現金流為" + ("正" if cf["latest"]["ocf"] > 0 else "負") + "，" +
            ("本業有實際現金流入。" if cf["latest"]["ocf"] > 0 else "本業現金流出，需留意資金壓力。"))
    fh = p.get("foreign_hold") or {}
    if fh.get("ok") and fh.get("chg5") is not None and abs(fh["chg5"]) >= 0.1:
        add("good" if fh["chg5"] > 0 else "warn", f"外資持股比率 {fh['latest']['ratio']}%，近 5 日{'增加' if fh['chg5'] > 0 else '減少'} {abs(fh['chg5'])} 個百分點。")
    dt = p.get("daytrade") or {}
    if dt.get("ok") and dt["days"][-1].get("ratio") is not None and dt["days"][-1]["ratio"] >= 30:
        add("warn", f"當沖比率 {dt['days'][-1]['ratio']}%，短線投機氣氛濃，隔日波動容易放大。")
    return out


def stock_pack(code: str) -> Dict:
    code = _clean_code(code)
    if not code:
        return {"error": "股票代號格式不正確"}
    futs = {name: _POOL.submit(fn, code) for name, fn in SECTIONS.items()}
    out = {"code": code, "source": "FinMind", "at": time.time()}
    for name, f in futs.items():
        try:
            out[name] = f.result(timeout=40)
        except Exception as e:
            logger.warning("finmind %s %s: %s", code, name, e)
            out[name] = {"ok": False, "error": type(e).__name__}
    try:
        out["indicators"] = sec_indicators(code)
    except Exception as e:
        logger.warning("finmind indicators %s: %s", code, e)
        out["indicators"] = {"ok": False, "error": type(e).__name__}
    out["insights"] = insights(out)
    dates = [out[k].get("date") for k in ("chips", "margin", "valuation") if isinstance(out.get(k), dict) and out[k].get("date")]
    out["latest_date"] = max(dates) if dates else None
    out["budget_left"] = budget_left()
    out["any_stale"] = any(isinstance(v, dict) and v.get("stale") for v in out.values())
    out["failed"] = [k for k in SECTIONS if not (out.get(k) or {}).get("ok")]
    return out


# ───────── 大盤與期貨 ─────────
_EQ = {"TX": 1.0, "MTX": 0.25, "TMF": 0.05}
_WHO = {"外資": "外資及陸資", "外資及陸資": "外資及陸資", "投信": "投信", "自營商": "自營商"}


def futures_inst(days: int = 12) -> Dict:
    """三大法人期貨未平倉（大台等效），格式與 taifex.parse_futures 相同：{date, items, history}。"""
    by_day: Dict[str, Dict[str, Dict]] = {}
    ok_any, stale, err = False, False, None
    for fid, w in _EQ.items():
        res = fm_get("TaiwanFuturesInstitutionalInvestors", fid, _day(-days))
        if res["ok"] and res["rows"]:
            ok_any = True
        stale = stale or res["stale"]
        err = err or res["error"]
        for r in res["rows"]:
            who = _WHO.get(r.get("institutional_investors"))
            lo, sh = _f(r.get("long_open_interest_balance_volume")), _f(r.get("short_open_interest_balance_volume"))
            if not who or lo is None or sh is None:
                continue
            d = str(r["date"])[:10]
            it = by_day.setdefault(d, {}).setdefault(who, {"tx_net_oi": 0.0, "equiv_net_oi": 0.0, "tx_long": 0.0, "tx_short": 0.0})
            it["equiv_net_oi"] += (lo - sh) * w
            if fid == "TX":
                it["tx_net_oi"], it["tx_long"], it["tx_short"] = lo - sh, lo, sh
    # 三種契約都要有才算完整的一天（只抓到大台就當作完整會低估）
    hist = []
    for d in sorted(by_day):
        items = by_day[d]
        if len(items) < 3:
            continue
        for it in items.values():
            it["equiv_net_oi"] = round(it["equiv_net_oi"])
        hist.append({"date": d, "items": items})
    if not hist:
        return {"ok": False, "date": None, "items": {}, "error": err}
    return {"ok": ok_any, "date": hist[-1]["date"], "items": hist[-1]["items"], "history": hist[-6:],
            "stale": stale, "error": err}


def futures_price(days: int = 12) -> Dict:
    """台指期近月日盤收盤／漲跌／成交量／未平倉；夜盤另列。休市日的 0 值占位列一律略過。"""
    res = fm_get("TaiwanFuturesDaily", "TX", _day(-days))
    reg: Dict[str, Dict] = {}
    night: Dict[str, Dict] = {}
    for r in res["rows"]:
        cd = str(r.get("contract_date") or "")
        c, v = _f(r.get("close")), _f(r.get("volume"))
        if "/" in cd or not c or c <= 0 or not v or v <= 0:
            continue
        tgt = night if r.get("trading_session") == "after_market" else reg
        d = str(r["date"])[:10]
        if d not in tgt or cd < tgt[d]["contract"]:
            tgt[d] = {"date": d, "contract": cd, "close": c, "open": _f(r.get("open")), "high": _f(r.get("max")),
                      "low": _f(r.get("min")), "chg": _f(r.get("spread")), "chg_pct": _f(r.get("spread_per")),
                      "volume": int(v), "oi": int(_f(r.get("open_interest")) or 0),
                      "settle": _f(r.get("settlement_price")) or None}
    days_ = [reg[d] for d in sorted(reg)]
    out = _sec(res, days_)
    if days_:
        out.update({"date": days_[-1]["date"], "latest": days_[-1], "days": days_[-6:],
                    "night": night[sorted(night)[-1]] if night else None})
    return out


def market_pack() -> Dict:
    """大盤三大法人（億元）、融資融券、台指期。"""
    f_inst = _POOL.submit(fm_get, "TaiwanStockTotalInstitutionalInvestors", None, _day(-16))
    f_margin = _POOL.submit(fm_get, "TaiwanStockTotalMarginPurchaseShortSale", None, _day(-16))
    f_fut = _POOL.submit(futures_price)
    f_fi = _POOL.submit(futures_inst)
    out = {"source": "FinMind", "at": time.time()}
    try:
        res = f_inst.result(timeout=40)
        days = []
        for d, rs in sorted(_by_date(res["rows"]).items()):
            v = {"foreign": 0.0, "trust": 0.0, "dealer": 0.0}
            for r in rs:
                net = ((_f(r.get("buy")) or 0) - (_f(r.get("sell")) or 0)) / 1e8
                n = r.get("name")
                if n in ("Foreign_Investor", "Foreign_Dealer_Self"):
                    v["foreign"] += net
                elif n == "Investment_Trust":
                    v["trust"] += net
                elif n in ("Dealer_self", "Dealer_Hedging"):
                    v["dealer"] += net
            days.append({"date": d, **{k: round(x, 1) for k, x in v.items()}, "total": round(sum(v.values()), 1)})
        out["inst"] = _sec(res, days, {"days": days[-8:], "date": days[-1]["date"] if days else None})
    except Exception as e:
        out["inst"] = {"ok": False, "error": type(e).__name__}
    try:
        res = f_margin.result(timeout=40)
        days = []
        for d, rs in sorted(_by_date(res["rows"]).items()):
            m = {r.get("name"): r for r in rs}
            mp, sp, mm = m.get("MarginPurchase"), m.get("ShortSale"), m.get("MarginPurchaseMoney")
            if not (mp and sp):
                continue
            tb, yb = _f(mp.get("TodayBalance")), _f(mp.get("YesBalance"))
            ts, ys = _f(sp.get("TodayBalance")), _f(sp.get("YesBalance"))
            days.append({"date": d, "margin_lots": int(tb or 0), "margin_chg": int((tb or 0) - (yb or 0)),
                         "short_lots": int(ts or 0), "short_chg": int((ts or 0) - (ys or 0)),
                         "margin_money_100m": round((_f(mm.get("TodayBalance")) or 0) / 1e8, 1) if mm else None})
        out["margin"] = _sec(res, days, {"days": days[-8:], "date": days[-1]["date"] if days else None})
    except Exception as e:
        out["margin"] = {"ok": False, "error": type(e).__name__}
    for k, f in (("futures", f_fut), ("futures_inst", f_fi)):
        try:
            out[k] = f.result(timeout=40)
        except Exception as e:
            out[k] = {"ok": False, "error": type(e).__name__}
    out["budget_left"] = budget_left()
    return out


def warm_market() -> Dict:
    """排程用：預先抓大盤資料放進快取，使用者打開網站時直接命中，不吃額度。"""
    try:
        p = market_pack()
        return {"ok": True, "fut_date": (p.get("futures") or {}).get("date"),
                "inst_date": (p.get("futures_inst") or {}).get("date")}
    except Exception as e:
        logger.warning("finmind warm_market: %s", e)
        return {"ok": False, "error": type(e).__name__}
