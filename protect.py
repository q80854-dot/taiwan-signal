"""統一防護層：流量湧入、濫用請求與錯誤外洩的最後一道關卡。

做四件事（都不改既有路由的程式）：
1. 每個 IP 的全域頻率限制（/api 與 /auth），重端點另外有更嚴的額度，且排在登入檢查之前，未登入的灌流量也會被擋。
2. 參數檢查：代號格式、limit 上限，避免被拿來打外部資料源或一次撈爆資料庫。
3. 短時間回應快取：同一份「全站共通」資料 10～60 秒內只算一次，湧入的人再多也只打一次資料庫／外部來源。
4. 重端點並行上限：同時最多 N 個重運算，超過直接回 503 請稍後，而不是把全部執行緒佔滿讓整站卡死。
另外：5xx 的 JSON 一律換成固定訊息，不把例外內容回給使用者。
"""
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict, deque

from flask import g, jsonify, request, Response

logger = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[0-9A-Za-z]{1,8}(\.(TW|TWO))?$", re.IGNORECASE)
_TICKER_PATHS = ("/api/instruments/", "/api/quote/", "/api/fundamentals/")

# 全站共通資料（不隨使用者而異）才能快取；/api/my、/api/me、/api/admin 等絕不在此列
_CACHE_TTL = {
    "/api/public/pulse": 5,
    "/api/signals": 15, "/api/market": 20, "/api/universe": 60, "/api/quote_status": 15,
    "/api/intraday": 20, "/api/freshness": 20, "/api/taifex": 60, "/api/market_extras": 60, "/api/calendar": 300,
    "/api/screener": 30, "/api/instruments/": 60, "/api/performance": 60, "/api/positions": 20,
    "/api/events": 20, "/api/news": 120, "/api/material_news": 120, "/api/fundamentals": 300,
    "/api/analysis": 120, "/api/fundamentals_radar": 300, "/api/quote/": 10,
}
_HEAVY = ("/api/screener", "/api/instruments/", "/api/fundamentals", "/api/news", "/api/material_news",
          "/api/market/inst-streak", "/api/market/inst-rank", "/api/analysis")

RATE_LIMIT = 240          # 每 IP 每分鐘（所有 /api、/auth）
HEAVY_LIMIT = 40          # 每 IP 每分鐘（重端點）
HEAVY_CONCURRENCY = 6     # 同時間最多幾個重運算
CACHE_MAX = 300

_rate, _heavy_rate = {}, {}
_cache: "OrderedDict[str, tuple]" = OrderedDict()
_cache_lock = threading.Lock()
_sem = threading.BoundedSemaphore(HEAVY_CONCURRENCY)
STATS = {"cache_hit": 0, "cache_miss": 0, "limited": 0, "busy": 0, "bad_param": 0}


def _ip() -> str:
    return (request.headers.get("CF-Connecting-IP") or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "?")


def _hit(bucket: dict, key: str, limit: int, window: int = 60) -> bool:
    now = time.time()
    q = bucket.get(key)
    if q is None:
        if len(bucket) > 8000:
            bucket.clear()
        q = bucket[key] = deque()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _is_heavy(p: str) -> bool:
    return p.startswith(_HEAVY)


def _ttl(p: str):
    best = None
    for k, v in _CACHE_TTL.items():
        if p == k or p.startswith(k if k.endswith("/") else k + "/") or p == k.rstrip("/"):
            if best is None or len(k) > best[0]:
                best = (len(k), v)
    return best[1] if best else None


def _reject(status: int, msg: str, retry: int = 0):
    r = jsonify({"error": msg})
    r.status_code = status
    if retry:
        r.headers["Retry-After"] = str(retry)
    return r


def first_guard():
    """最先執行：頻率限制＋參數檢查（不碰資料庫，所以灌再多請求也很便宜）。"""
    p = request.path
    if not (p.startswith("/api/") or p.startswith("/auth/")):
        return None
    ip = _ip()
    if not _hit(_rate, ip, RATE_LIMIT):
        STATS["limited"] += 1
        return _reject(429, "請求過於頻繁，請稍後再試", 30)
    if request.method == "GET" and _is_heavy(p) and not _hit(_heavy_rate, ip, HEAVY_LIMIT):
        STATS["limited"] += 1
        return _reject(429, "查詢過於頻繁，請稍後再試", 30)
    for pre in _TICKER_PATHS:
        if p.startswith(pre):
            t = p[len(pre):]
            if not _TICKER_RE.match(t):
                STATS["bad_param"] += 1
                return _reject(400, "代號格式不正確")
    lim = request.args.get("limit")
    if lim is not None and not (lim.isdigit() and 0 < int(lim) <= 500):
        STATS["bad_param"] += 1
        return _reject(400, "limit 需為 1～500 的整數")
    return None


def _cache_key() -> str:
    # 站主與一般會員看到的內容不同（會員版移除本金相關欄位），快取必須分開存
    try:
        import members
        role = "o" if members.viewer_is_owner() else "m"
    except Exception:
        role = "m"
    return role + "|" + request.path + "?" + "&".join(f"{k}={v}" for k, v in sorted(request.args.items(multi=True)))


def after_auth_guard():
    """登入檢查之後執行：先看快取；沒有就搶重運算名額。"""
    if request.method != "GET":
        return None
    p = request.path
    ttl = _ttl(p)
    if ttl:
        key = _cache_key()
        now = time.time()
        with _cache_lock:
            ent = _cache.get(key)
            if ent and ent[0] > now:
                _cache.move_to_end(key)
                STATS["cache_hit"] += 1
                r = Response(ent[2], status=200, content_type=ent[1])
                r.headers["X-Cache"] = "HIT"
                return r
        STATS["cache_miss"] += 1
        g._pc_key, g._pc_ttl = key, ttl
    if _is_heavy(p):
        if not _sem.acquire(timeout=3):
            STATS["busy"] += 1
            return _reject(503, "目前查詢人數較多，請 5 秒後再試", 5)
        g._pc_sem = True
    return None


def after(resp):
    try:
        key = getattr(g, "_pc_key", None)
        if key and resp.status_code == 200 and not resp.direct_passthrough and len(resp.get_data()) < 600_000:
            with _cache_lock:
                _cache[key] = (time.time() + g._pc_ttl, resp.content_type, resp.get_data())
                _cache.move_to_end(key)
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)
        # 5xx 的 JSON 不外洩例外內容
        if resp.status_code >= 500 and request.path.startswith("/api/") and (resp.content_type or "").startswith("application/json"):
            logger.warning(f"5xx {request.method} {request.path}: {resp.get_data(as_text=True)[:200]}")
            body = jsonify({"error": "伺服器暫時無法處理，請稍後再試"})
            resp.set_data(body.get_data())
            resp.headers["Content-Length"] = str(len(resp.get_data()))
    except Exception as e:
        logger.warning(f"protect.after: {e}")
    return resp


def teardown(_exc):
    if getattr(g, "_pc_sem", False):
        g._pc_sem = False
        try:
            _sem.release()
        except ValueError:
            pass


def clear_cache():
    with _cache_lock:
        _cache.clear()


def install(app):
    """在 members.gate 註冊『之後』呼叫：first_guard 插到最前面，快取與名額檢查接在登入檢查之後。"""
    app.before_request_funcs.setdefault(None, []).insert(0, first_guard)
    app.before_request(after_auth_guard)
    app.after_request(after)
    app.teardown_request(teardown)
