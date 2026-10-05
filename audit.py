"""
資料稽核：把系統內部使用的日K，逐日對照證交所(TWSE)／櫃買中心(TPEx)官方個股日成交資料。
目的：讓使用者直接在網頁上看到「我們的數據 vs 官方數據」是否對得上，不靠猜測。

分類：
  match      開高低收皆與官方一致（容許 0.011 元四捨五入誤差）
  adjusted   整段價格與官方呈同一固定比例（Yahoo 還原權值／除息調整後價格），形狀一致僅水準不同
  mismatch   其他：應視為資料異常
"""
import time, logging, threading
from datetime import datetime, timedelta
import requests

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_TOL = 0.011
_cache = {"ts": 0, "data": None}
_lock = threading.Lock()
_TTL = 1800

SOURCES = [
    {"name": "日K／週K／60分K", "source": "Yahoo Finance (yfinance)", "endpoint": "auto_adjust=True",
     "update": "每次掃描增量更新並寫入資料庫快取", "coverage": "全部上市櫃掃描池",
     "limits": "價格為除權息還原價，與官方未還原收盤價在配息股上會有固定比例差；成交量與官方有差異，僅作參考"},
    {"name": "全市場報價／掃描池", "source": "證交所 STOCK_DAY_ALL + 櫃買 tpex_mainboard_quotes", "endpoint": "官方 OpenAPI",
     "update": "收盤後更新（交易日）", "coverage": "上市＋上櫃",
     "limits": "掃描池條件：收盤價>5 元、單日成交量≥200 張；排除處置／注意股與非普通股代碼"},
    {"name": "最後一根K棒校正", "source": "官方收盤價", "endpoint": "掃描時以官方報價覆蓋 Yahoo 末棒",
     "update": "每次掃描", "coverage": "掃描池", "limits": "官方報價日期與 Yahoo 末棒日期須相同才校正"},
    {"name": "月營收", "source": "證交所 t187ap05_L／櫃買 mopsfin_t187ap05_O", "endpoint": "OpenAPI",
     "update": "每月 10 日前後公告", "coverage": "上市＋上櫃", "limits": "僅最新一期，歷史期數由系統逐月累積"},
    {"name": "本益比／股價淨值比／殖利率", "source": "證交所 BWIBBU_ALL／櫃買 peratio_analysis", "endpoint": "OpenAPI",
     "update": "每日", "coverage": "上市＋上櫃", "limits": "虧損公司本益比為空"},
    {"name": "獲利能力", "source": "證交所／櫃買 t187ap06 系列", "endpoint": "OpenAPI",
     "update": "每季", "coverage": "上市＋上櫃", "limits": "金融業科目不同，不納入雷達排名"},
    {"name": "三大法人買賣超", "source": "證交所 T86", "endpoint": "個股逐日",
     "update": "收盤後", "coverage": "僅上市", "limits": "上櫃個股無法人資料，不加減分"},
    {"name": "重大訊息", "source": "證交所 t187ap04_L", "endpoint": "OpenAPI",
     "update": "快取 10～15 分鐘", "coverage": "僅上市", "limits": "上櫃重大訊息未涵蓋"},
]


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("X", "").strip())
    except Exception:
        return None


def _roc_to_iso(s):
    try:
        y, m, d = str(s).strip().split("/")
        return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return None


def _official_month(code, is_otc, year, month):
    """回傳 {iso_date: {open,high,low,close,volume_lots}}"""
    out = {}
    if is_otc:
        url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
        r = requests.get(url, params={"code": code, "date": f"{year}/{month:02d}/01", "response": "json"},
                         headers=HEADERS, timeout=15)
        tbs = r.json().get("tables") or [{}]
        for row in (tbs[0].get("data") or []):
            d = _roc_to_iso(row[0])
            if not d:
                continue
            out[d] = {"open": _num(row[3]), "high": _num(row[4]), "low": _num(row[5]),
                      "close": _num(row[6]), "volume_lots": (_num(row[1]) or 0)}  # 成交張數
    else:
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
        r = requests.get(url, params={"response": "json", "date": f"{year}{month:02d}01", "stockNo": code},
                         headers=HEADERS, timeout=15)
        for row in (r.json().get("data") or []):
            d = _roc_to_iso(row[0])
            if not d:
                continue
            out[d] = {"open": _num(row[3]), "high": _num(row[4]), "low": _num(row[5]),
                      "close": _num(row[6]), "volume_lots": (_num(row[1]) or 0) / 1000.0}
    return out


def audit_ticker(ticker, n=10):
    from state_store import store
    code = ticker.split(".")[0]
    is_otc = ticker.endswith(".TWO")
    bars = [b for b in store.get_cached_ohlcv_bars(ticker, "daily", limit=n + 5) if (b.get("volume") or 0) > 0][-n:]
    res = {"ticker": ticker, "code": code, "market": "上櫃" if is_otc else "上市", "n": len(bars), "rows": []}
    if not bars:
        res.update(status="nodata", note="資料庫無此股日K快取"); return res
    months = sorted({(int(b["bar_date"][:4]), int(b["bar_date"][5:7])) for b in bars})
    off = {}
    try:
        for i, (y, m) in enumerate(months):
            off.update(_official_month(code, is_otc, y, m))
            if i < len(months) - 1: time.sleep(1.2)
    except Exception as e:
        res.update(status="error", note=f"官方資料取得失敗：{e}"); return res
    ratios, exact, cmp_n = [], 0, 0
    for b in bars:
        o = off.get(b["bar_date"])
        row = {"date": b["bar_date"], "our_close": b["close"], "our_volume": b["volume"]}
        if o and o.get("close"):
            cmp_n += 1
            diffs = [abs(b[k] - o[k]) for k in ("open", "high", "low", "close") if o.get(k) is not None]
            row.update(off_close=o["close"], off_volume=round(o["volume_lots"]), diff_close=round(b["close"] - o["close"], 2),
                       exact=max(diffs) <= _TOL,
                       vol_ratio=round(b["volume"] / o["volume_lots"], 3) if o["volume_lots"] else None)
            if row["exact"]: exact += 1
            ratios.append(b["close"] / o["close"])
        else:
            row.update(off_close=None, note="官方無此日資料")
        res["rows"].append(row)
    res["compared"] = cmp_n
    res["exact"] = exact
    if cmp_n == 0:
        res.update(status="nodata", note="官方資料無可比對日期")
    elif exact == cmp_n:
        res["status"] = "match"
    else:
        spread = (max(ratios) - min(ratios)) / (sum(ratios) / len(ratios))
        res["status"] = "adjusted" if spread < 0.004 else "mismatch"
        res["ratio"] = round(sum(ratios) / len(ratios), 4)
        res["note"] = ("價格與官方呈固定比例，屬除權息還原調整，形狀一致" if res["status"] == "adjusted"
                       else "價格與官方不同步，需檢查快取")
    vr = [r["vol_ratio"] for r in res["rows"] if r.get("vol_ratio")]
    res["vol_ratio_avg"] = round(sum(vr) / len(vr), 3) if vr else None
    return res


def sample_tickers():
    from state_store import store
    base = ["2330.TW", "2317.TW", "2454.TW", "2886.TW", "2603.TW", "6488.TWO", "3293.TWO"]
    extra = []
    try:
        for r in store.get_recent_signals(limit=30, days_back=14):
            t = r.get("ticker")
            if t and t not in base and t not in extra: extra.append(t)
    except Exception:
        pass
    return (base + extra)[:12]


def run_audit(force=False, tickers=None):
    with _lock:
        if not force and not tickers and _cache["data"] and time.time() - _cache["ts"] < _TTL:
            return _cache["data"]
        items = []
        for t in (tickers or sample_tickers()):
            try:
                items.append(audit_ticker(t))
            except Exception as e:
                items.append({"ticker": t, "status": "error", "note": str(e), "rows": []})
            time.sleep(1.2)
        summ = {k: sum(1 for i in items if i["status"] == k) for k in ("match", "adjusted", "mismatch", "nodata", "error")}
        data = {"generated_at": (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"), "items": items, "summary": summ,
                "sources": SOURCES}
        if not tickers:
            _cache.update(ts=time.time(), data=data)
        return data


_state = {"running": False, "started": 0}

def get_audit(refresh=False):
    """非阻塞：有新鮮快取就直接回；否則背景執行並回 running=True（前端輪詢）。"""
    fresh = _cache["data"] and time.time() - _cache["ts"] < _TTL
    if fresh and not refresh:
        return dict(_cache["data"], running=False)
    if not _state["running"] or time.time() - _state["started"] > 300:
        _state.update(running=True, started=time.time())
        def _job():
            try:
                run_audit(force=True)
            except Exception as e:
                logger.error(f"audit job: {e}")
            finally:
                _state["running"] = False
        threading.Thread(target=_job, daemon=True).start()
    base = _cache["data"] or {"items": [], "summary": {}, "sources": SOURCES, "generated_at": None}
    return dict(base, running=True)
