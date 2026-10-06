"""
管理後台登入保護（單一使用者，不做帳號系統）。

設計：
- 密碼只存在 Render 環境變數 ADMIN_PASSWORD（至少 8 碼），不在程式碼、不在 GitHub。沒設定 = 後台鎖死（fail closed）。
- 登入成功後發一張「簽名 cookie」（工作階段 cookie，關閉瀏覽器即失效；伺服器端 5 分鐘無動作即到期（有操作時網頁每分鐘心跳順延）；網頁離開策略學習頁或關閉分頁時會主動登出，所以每次進來都要重新輸入密碼）：內容是到期時間 + HMAC-SHA256 簽章，金鑰由密碼衍生，
  所以改密碼會讓所有舊登入立刻失效。cookie 為 HttpOnly（網頁腳本讀不到）、Secure、SameSite=Strict。
- 防暴力破解：同一來源 15 分鐘內錯 5 次、或全站 15 分鐘內錯 30 次就暫時鎖住；每次錯誤另延遲 0.6 秒。
- 密碼比對使用固定時間比較，避免從回應時間猜密碼。
注意：限流記在各 worker 記憶體內（重啟會清空），所以請使用夠長的密碼，不要只靠限流。
"""
import os, time, hmac, hashlib, threading
from functools import wraps
from flask import request, jsonify

# 2026-10-06：依使用者要求暫時關閉後台登入保護（預設不需密碼）。
# 要重新啟用：在 Render 環境變數設 ADMIN_AUTH_REQUIRED=1（並維持 ADMIN_PASSWORD 至少 8 碼）。
REQUIRED = os.environ.get("ADMIN_AUTH_REQUIRED", "0") == "1"
COOKIE = "ts_admin"
TTL = 5 * 60    # 5 分鐘沒有動作就失效（每次後台請求/網頁心跳會順延）；且是『工作階段 cookie』，關閉瀏覽器就消失
PROTECTED_PREFIXES = ('/api/health', '/api/settings', '/api/audit', '/api/diagnostics')
WINDOW = 15 * 60
PER_IP_MAX = 5
GLOBAL_MAX = 30
_fails = {}
_gfails = []
_lock = threading.Lock()


def _pw():
    return os.environ.get("ADMIN_PASSWORD", "")


def configured():
    return len(_pw()) >= 8


def _key():
    return hashlib.sha256(("taiwan-signal-admin-v1|" + _pw()).encode()).digest()


def _sign(exp):
    return hmac.new(_key(), str(exp).encode(), hashlib.sha256).hexdigest()


def issue():
    exp = int(time.time()) + TTL
    return f"{exp}.{_sign(exp)}"


def valid(tok):
    if not configured() or not tok or "." not in tok:
        return False
    try:
        exp, sig = tok.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))
    except Exception:
        return False


def authed():
    if not REQUIRED:
        return True
    return valid(request.cookies.get(COOKIE, ""))


def _ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() or request.remote_addr or "?")


def _secure():
    return request.headers.get("X-Forwarded-Proto", request.scheme) == "https"


def _retry_after(ip):
    now = time.time()
    with _lock:
        _gfails[:] = [t for t in _gfails if now - t < WINDOW]
        lst = [t for t in _fails.get(ip, []) if now - t < WINDOW]
        _fails[ip] = lst
        if len(lst) >= PER_IP_MAX:
            return int(WINDOW - (now - lst[0])) + 1
        if len(_gfails) >= GLOBAL_MAX:
            return int(WINDOW - (now - _gfails[0])) + 1
    return 0


def _record_fail(ip):
    now = time.time()
    with _lock:
        _fails.setdefault(ip, []).append(now)
        _gfails.append(now)


def status():
    return jsonify({"configured": configured() or not REQUIRED, "authed": authed(), "required": REQUIRED})


def login():
    if not REQUIRED:
        return jsonify({"ok": True, "required": False})
    if not request.is_json:
        return jsonify({"error": "格式錯誤"}), 400
    if not configured():
        return jsonify({"error": "後台尚未啟用：請先在 Render 設定環境變數 ADMIN_PASSWORD（至少 8 碼）", "configured": False}), 503
    ip = _ip()
    wait = _retry_after(ip)
    if wait:
        return jsonify({"error": f"嘗試次數過多，請 {wait // 60 + 1} 分鐘後再試", "locked": True}), 429
    given = str((request.get_json(silent=True) or {}).get("password", ""))
    ok = hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(_pw().encode()).digest())
    if not ok:
        _record_fail(ip)
        time.sleep(0.6)
        return jsonify({"error": "密碼不正確"}), 401
    with _lock:
        _fails.pop(ip, None)
    resp = jsonify({"ok": True})
    _set(resp)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _set(resp):
    resp.set_cookie(COOKIE, issue(), httponly=True, secure=_secure(), samesite="Strict", path="/")


def _deny():
    resp = jsonify({"error": "需要登入", "auth": False, "configured": configured()})
    resp.status_code = 401
    resp.headers["Cache-Control"] = "no-store"
    return resp


def guard():
    """before_request：系統檢查／設定／稽核／診斷等後台 API 一律需登入。"""
    if request.path.startswith(PROTECTED_PREFIXES) and not authed():
        return _deny()
    return None


def after(resp):
    """after_request：後台 API 回應一律不快取；已登入的請求順延 5 分鐘無動作期限。"""
    try:
        if request.path.startswith(PROTECTED_PREFIXES) or request.path.startswith("/api/admin/"):
            resp.headers["Cache-Control"] = "no-store"
            if REQUIRED and authed() and request.path != "/api/admin/logout":
                _set(resp)
    except Exception:
        pass
    return resp


def ping():
    """網頁有操作時每分鐘呼叫一次，順延無動作期限；未登入回 401。"""
    if not authed():
        return _deny()
    return jsonify({"ok": True, "ttl": TTL})


def logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not authed():
            return _deny()
        out = f(*a, **kw)
        try:
            if hasattr(out, "headers"):
                out.headers["Cache-Control"] = "no-store"
        except Exception:
            pass
        return out
    return wrapper
