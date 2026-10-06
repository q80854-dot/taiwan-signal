"""
analysis_foreign.py — 外資買賣超門檻回測（研究用）
用途：檢驗 risk_manager.check_foreign_flow 的門檻（賣超 20 億降信心、50 億暫停多單）
是否真的對「之後的大盤走勢」有預測力。
資料：TWSE BFI82U 三大法人買賣金額統計（逐日，官方）＋ TWSE MI_5MINS_HIST 加權指數日收盤。
"""
import time, logging, threading
from datetime import datetime, timedelta, timezone
import requests

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0"}
_lock = threading.Lock()
_state = {"running": False, "started": None, "progress": "", "error": None}


def _bfi82u(date_str):
    """回傳某日外資（不含外資自營商）買賣差額（元）；非交易日回 None。"""
    url = f"https://www.twse.com.tw/fund/BFI82U?response=json&dayDate={date_str}&type=day"
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(5, 20))
            if r.status_code != 200:
                continue
            d = r.json()
            if d.get("stat") != "OK":
                return None
            row = next((x for x in d.get("data", []) if x[0].strip().startswith("外資及陸資")), None)
            if row and len(row) >= 4:
                return int(row[3].replace(",", "").replace("+", ""))
            return None
        except Exception as e:
            logger.warning(f"bfi82u {date_str}: {e}")
            time.sleep(2)
    return None


def _taiex_month(yyyymm):
    url = f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={yyyymm}01&response=json"
    out = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=(5, 20))
        d = r.json()
        if d.get("stat") != "OK":
            return out
        for row in d.get("data", []):
            y, m, dd = row[0].split("/")
            iso = f"{int(y) + 1911:04d}-{int(m):02d}-{int(dd):02d}"
            out[iso] = float(row[4].replace(",", ""))
    except Exception as e:
        logger.warning(f"taiex {yyyymm}: {e}")
    return out


def _bucket(nb_yi):
    if nb_yi <= -50: return "≤ -50億（暫停多單）"
    if nb_yi <= -20: return "-50 ~ -20億（降信心）"
    if nb_yi < 0:    return "-20 ~ 0億"
    if nb_yi < 20:   return "0 ~ +20億"
    return "≥ +20億（偏多）"


ORDER = ["≤ -50億（暫停多單）", "-50 ~ -20億（降信心）", "-20 ~ 0億", "0 ~ +20億", "≥ +20億（偏多）"]


def _corr(xs, ys):
    n = len(xs)
    if n < 5: return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0: return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5, 3)


def compute(want_days=100):
    from state_store import store
    hist = store.get_meta("bfi82u_hist", {}) or {}
    d = datetime.now(timezone.utc) + timedelta(hours=8)
    tried = 0
    while len([k for k, v in hist.items() if v is not None]) < want_days and tried < want_days * 2:
        if d.weekday() < 5:
            iso = d.strftime("%Y-%m-%d")
            if iso not in hist:
                tried += 1
                hist[iso] = _bfi82u(d.strftime("%Y%m%d"))
                _state["progress"] = f"外資金額 {len(hist)} 天"
                time.sleep(2.5)
        d -= timedelta(days=1)
    store.set_meta("bfi82u_hist", hist)
    days = sorted(k for k, v in hist.items() if v is not None)[-want_days:]
    if len(days) < 20:
        return {"error": "可用交易日太少", "days": len(days)}
    months = sorted({k[:7].replace("-", "") for k in days})
    # 多抓一個月，讓最後幾天也有「之後」的報酬
    px = {}
    for m in months:
        px.update(_taiex_month(m)); time.sleep(1.5)
    tdays = sorted(px)
    rows = []
    for k in days:
        if k not in px: continue
        i = tdays.index(k)
        r0 = (px[k] / px[tdays[i - 1]] - 1) * 100 if i >= 1 else None
        f = lambda n: (px[tdays[i + n]] / px[k] - 1) * 100 if i + n < len(tdays) else None
        rows.append({"date": k, "net_yi": round(hist[k] / 1e8, 1), "r0": r0, "r1": f(1), "r3": f(3), "r5": f(5)})
    avg = lambda a: round(sum(a) / len(a), 3) if a else None
    def summ(rs):
        r1 = [r["r1"] for r in rs if r["r1"] is not None]; r3 = [r["r3"] for r in rs if r["r3"] is not None]; r5 = [r["r5"] for r in rs if r["r5"] is not None]
        r0 = [r["r0"] for r in rs if r["r0"] is not None]
        return {"n": len(rs), "same_day": avg(r0), "next1": avg(r1), "next3": avg(r3), "next5": avg(r5),
                "next1_down_pct": round(sum(1 for x in r1 if x < 0) / len(r1) * 100) if r1 else None}
    buckets = {}
    for r in rows: buckets.setdefault(_bucket(r["net_yi"]), []).append(r)
    xs = [r["net_yi"] for r in rows if r["r1"] is not None]
    return {
        "days": len(rows), "from": rows[0]["date"], "to": rows[-1]["date"],
        "baseline": summ(rows),
        "buckets": [{"bucket": b, **summ(buckets[b])} for b in ORDER if b in buckets],
        "corr_same_day": _corr([r["net_yi"] for r in rows if r["r0"] is not None], [r["r0"] for r in rows if r["r0"] is not None]),
        "corr_next1": _corr(xs, [r["r1"] for r in rows if r["r1"] is not None]),
        "corr_next5": _corr([r["net_yi"] for r in rows if r["r5"] is not None], [r["r5"] for r in rows if r["r5"] is not None]),
        "worst_days": sorted(rows, key=lambda r: r["net_yi"])[:6],
    }


def start():
    """啟動背景計算（一次只跑一個），結果存進 meta。"""
    from state_store import store
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update({"running": True, "started": time.time(), "error": None, "progress": "啟動"})
    def _run():
        try:
            res = compute()
            res["computed_at"] = time.time()
            store.set_meta("analysis_foreign_flow", res)
        except Exception as e:
            logger.exception("analysis_foreign")
            _state["error"] = str(e)[:200]
        finally:
            _state["running"] = False
    threading.Thread(target=_run, daemon=True).start()
    return dict(_state)


def status():
    from state_store import store
    return {"state": dict(_state), "result": store.get_meta("analysis_foreign_flow")}
