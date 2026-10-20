"""資料來源連線檢測：每一個外部來源（依主機分類）現在「連不連得上、多快、最近成功率」。

和 freshness.py 的分工：
- freshness.py 看的是「資料日期夠不夠新」（抓到的是哪一天）。
- source_health.py 看的是「連線本身健不健康」（最近幾次請求成功/失敗、延遲、最後成功時間）。

做法：在程式啟動時用 install() 把 requests.get/post 包一層計時與記錄，
所有 `requests.get(...)` 的呼叫都會自動被記錄，不需要改任何既有呼叫點。
只存在記憶體（每個主機保留最近 N 次），是即時營運面板，不寫資料庫。
"""
import logging
import threading
import time
from collections import deque
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RING = 40                       # 每個主機保留最近幾次請求
_hosts = {}                      # host -> state dict
_installed = False

# 主機 → 好讀名稱（面板顯示用；未列出的直接用主機名）
_FRIENDLY = {
    "openapi.twse.com.tw": "證交所 OpenAPI",
    "www.twse.com.tw": "證交所 TWSE",
    "mis.twse.com.tw": "證交所 MIS 即時報價",
    "mops.twse.com.tw": "公開資訊觀測站 MOPS",
    "mopsov.twse.com.tw": "公開資訊觀測站 MOPS",
    "www.tpex.org.tw": "櫃買中心 TPEX",
    "openapi.tpex.org.tw": "櫃買中心 OpenAPI",
    "www.taifex.com.tw": "期交所 TAIFEX",
    "query1.finance.yahoo.com": "Yahoo Finance",
    "query2.finance.yahoo.com": "Yahoo Finance",
    "api.coingecko.com": "CoinGecko（BTC／黃金）",
    "api.fugle.tw": "富果 Fugle",
    "api.finmindtrade.com": "FinMind 財報",
    "www.fubon.com": "富邦 公開報價",
}


def friendly(host):
    return _FRIENDLY.get(host, host)


def _host_of(url):
    try:
        return (urlparse(str(url)).hostname or "?").lower()
    except Exception:
        return "?"


def record(url, ok, ms, code=None, err=None):
    """記一次請求結果。ms 為毫秒。ok=True 代表連得上且 HTTP < 400。

    特例：HTTP 402／429＝免費額度／頻率達上限（例如 FinMind）。這代表「連線是通的、
    只是這個時段額度用完」，程式本身會熔斷＋改用快取優雅降級，不該被當成「連線中斷」。
    這裡把它記成獨立的『額度達上限』（連線視為可達，不列入連線失敗），健檢顯示成留意、
    而不是把整個健檢變成異常(紅)；真正的連線失敗（逾時、5xx、DNS）仍照舊標為中斷。"""
    host = _host_of(url)
    if host in ("?", "127.0.0.1", "localhost"):
        return
    limited = code in (402, 429)
    now = time.time()
    with _LOCK:
        h = _hosts.get(host)
        if h is None:
            h = _hosts[host] = {"ring": deque(maxlen=_RING), "lat": deque(maxlen=_RING),
                                "lim": deque(maxlen=_RING), "last_ok": None, "last_at": None,
                                "last_ms": None, "last_code": None, "last_err": None}
        # 連線健康度：成功或『額度達上限』都算「連得上」（ring=1）；只有真正連不上才記 0。
        h["ring"].append(1 if (ok or limited) else 0)
        h["lim"].append(1 if limited else 0)
        if ms is not None:
            h["lat"].append(float(ms))
        h["last_at"] = now
        h["last_ms"] = round(float(ms), 1) if ms is not None else None
        h["last_code"] = code
        if ok:
            h["last_ok"] = now
            h["last_err"] = None
        elif limited:
            h["last_err"] = f"額度/頻率達上限（HTTP {code}），已改用快取降級，非連線中斷"
        else:
            h["last_err"] = (str(err)[:160] if err else (f"HTTP {code}" if code else "連線失敗"))


def _state(ring, last_ok, lim=None):
    n = len(ring)
    if n == 0:
        return "unknown"
    lim = list(lim or [])
    rate = sum(ring) / n
    last = ring[-1]
    recent = list(ring)[-3:]
    recent_lim = lim[-3:]
    # 近幾次都是「額度達上限」（連得上、只是沒額度）→ 獨立標記為 limited，而非 down/warn
    if recent_lim and all(x == 1 for x in recent_lim):
        return "limited"
    if last_ok is None and all(x == 0 for x in ring) and not any(lim):
        return "down"                                   # 從頭到尾沒成功過（且非額度問題）
    if all(x == 0 for x in recent) and n >= 3:
        return "down"                                   # 連續（真正）失敗
    if last == 0 or rate < 0.9:
        return "warn"
    return "ok"


def _pct(ring):
    n = len(ring)
    return round(sum(ring) / n * 100) if n else None


def _p50(lat):
    xs = sorted(lat)
    return round(xs[len(xs) // 2], 1) if xs else None


def snapshot():
    """面板資料：每個主機的狀態、成功率、延遲、最後成功時間。"""
    now = time.time()
    out = []
    with _LOCK:
        for host, h in _hosts.items():
            ring = h["ring"]
            st = _state(ring, h["last_ok"], h.get("lim"))
            out.append({
                "host": host,
                "name": friendly(host),
                "state": st,
                "attempts": len(ring),
                "success_rate": _pct(ring),
                "p50_ms": _p50(h["lat"]),
                "last_ms": h["last_ms"],
                "last_code": h["last_code"],
                "last_success_ago": (int(now - h["last_ok"]) if h["last_ok"] else None),
                "last_attempt_ago": (int(now - h["last_at"]) if h["last_at"] else None),
                "last_err": h["last_err"] if st != "ok" else None,
            })
    order = {"down": 0, "limited": 1, "warn": 2, "unknown": 3, "ok": 4}
    out.sort(key=lambda r: (order.get(r["state"], 9), r["host"]))
    return {"at": now, "sources": out,
            "counts": {k: sum(1 for r in out if r["state"] == k) for k in ("ok", "warn", "down", "limited", "unknown")}}


def summary():
    """給 health.py 用的一句話總結。"""
    snap = snapshot()
    c = snap["counts"]
    total = sum(c.values())
    if total == 0:
        return "info", "本次啟動後尚未對外抓取任何來源"
    if c["down"]:
        downs = [r["name"] for r in snap["sources"] if r["state"] == "down"][:4]
        return "fail", f"{c['down']} 個來源連線異常：{'、'.join(downs)}（共監測 {total} 個來源）"
    if c.get("limited"):
        lims = [r["name"] for r in snap["sources"] if r["state"] == "limited"][:4]
        return "warn", f"{c['limited']} 個來源額度／頻率達上限：{'、'.join(lims)}（連線正常，已改用快取降級；共監測 {total} 個來源）"
    if c["warn"]:
        warns = [r["name"] for r in snap["sources"] if r["state"] == "warn"][:4]
        return "warn", f"{c['warn']} 個來源成功率偏低：{'、'.join(warns)}（共監測 {total} 個來源）"
    return "ok", f"{c['ok']}／{total} 個來源連線正常"


def install():
    """包住 requests.get / requests.post 做計時與記錄。只做一次，process 全域生效。"""
    global _installed
    if _installed:
        return
    try:
        import requests
    except Exception as e:
        logger.warning(f"source_health install: {e}")
        return
    if getattr(requests, "_sh_installed", False):
        _installed = True
        return
    orig_get, orig_post = requests.get, requests.post

    def _wrap(orig):
        def inner(url, *a, **k):
            t0 = time.time()
            code = err = None
            ok = False
            try:
                r = orig(url, *a, **k)
                code = getattr(r, "status_code", None)
                ok = bool(getattr(r, "ok", code and code < 400))
                return r
            except Exception as e:
                err = e
                raise
            finally:
                try:
                    record(url, ok, (time.time() - t0) * 1000.0, code, err)
                except Exception:
                    pass
        return inner

    requests.get = _wrap(orig_get)
    requests.post = _wrap(orig_post)
    requests._sh_installed = True
    _installed = True
    logger.info("source_health：已啟用資料來源連線檢測（requests.get/post 計時）")
