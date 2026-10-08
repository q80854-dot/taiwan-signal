"""會員（Google 登入）與每人自己的自選股。

設計：
- 登入只用 Google（OpenID Connect 授權碼流程）。我們不保管任何密碼。
- 需要 Render 環境變數：GOOGLE_CLIENT_ID、GOOGLE_CLIENT_SECRET（Google Cloud 建立的 OAuth 用戶端）。
  選填：PUBLIC_BASE_URL（例如 https://taiwan-signal.onrender.com，之後換自訂網域改這裡）、SESSION_SECRET（簽名金鑰，沒設就由 client secret 衍生）。
  沒設定 = 登入功能關閉（/api/me 回 login_enabled=false），網站其他功能照舊。
- 登入狀態：簽名 cookie ts_member（HttpOnly、Secure、SameSite=Lax，30 天），內容只有會員識別碼與到期時間。
- 授權流程防護：state（防 CSRF）＋ nonce（防重放），存在簽名 cookie ts_oauth（10 分鐘）；id_token 直接從 Google 的 token 端點（TLS）取得，並檢查 iss／aud／exp／nonce／email_verified。
- 變更類 API（POST／DELETE）一律要求 header X-TS: 1，並只接受已登入者。
- 資料表：members、member_groups、member_watch（Postgres／SQLite 共用語法）。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Blueprint, jsonify, redirect, request

logger = logging.getLogger(__name__)
bp = Blueprint("members", __name__)

COOKIE = "ts_member"
OAUTH_COOKIE = "ts_oauth"
SESSION_TTL = 30 * 24 * 3600
OAUTH_TTL = 600
MAX_GROUPS = 10
MAX_PER_GROUP = 100
DEFAULT_GROUP = "自選股"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_CODE_RE = re.compile(r"^[0-9A-Z]{4,7}$")
_ready = {"ok": False}


# ───────── 設定與簽名 ─────────
def client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def enabled() -> bool:
    return bool(client_id() and _client_secret())


def _key() -> bytes:
    base = os.environ.get("SESSION_SECRET", "").strip() or _client_secret()
    return hashlib.sha256(("taiwan-signal-members-v1|" + base).encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def unsign(tok: str):
    """簽名正確且未過期才回傳內容，否則 None。"""
    try:
        body, sig = (tok or "").rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        p = json.loads(_unb64(body))
        return p if p.get("exp", 0) > time.time() else None
    except Exception:
        return None


def _base_url() -> str:
    b = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if b:
        return b
    return request.url_root.rstrip("/").replace("http://", "https://", 1)


def _redirect_uri() -> str:
    return _base_url() + "/auth/google/callback"


# ───────── 資料庫 ─────────
def _store():
    from state_store import store
    return store


def ensure_tables():
    if _ready["ok"]:
        return
    ddl = """
        CREATE TABLE IF NOT EXISTS members (
            sub TEXT PRIMARY KEY, email TEXT, name TEXT, created_at TEXT, last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS member_groups (
            sub TEXT, grp TEXT, sort_no INTEGER, PRIMARY KEY (sub, grp)
        );
        CREATE TABLE IF NOT EXISTS member_watch (
            sub TEXT, grp TEXT, code TEXT, name TEXT, market TEXT, added_at TEXT, PRIMARY KEY (sub, grp, code)
        );
    """
    with _store()._conn() as conn:
        conn.executescript(ddl)
    _ready["ok"] = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_member(sub: str, email: str, name: str):
    ensure_tables()
    with _store()._conn() as conn:
        conn.execute(
            "INSERT INTO members (sub, email, name, created_at, last_login) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (sub) DO UPDATE SET email=EXCLUDED.email, name=EXCLUDED.name, last_login=EXCLUDED.last_login",
            (sub, email, name, _now(), _now()))
        conn.execute("INSERT INTO member_groups (sub, grp, sort_no) VALUES (?, ?, 0) ON CONFLICT (sub, grp) DO NOTHING",
                     (sub, DEFAULT_GROUP))


def get_member(sub: str):
    ensure_tables()
    with _store()._conn() as conn:
        r = conn.execute("SELECT sub, email, name FROM members WHERE sub=?", (sub,)).fetchone()
    return dict(r) if r else None


def groups_of(sub: str):
    ensure_tables()
    with _store()._conn() as conn:
        gs = [r["grp"] for r in conn.execute("SELECT grp FROM member_groups WHERE sub=? ORDER BY sort_no, grp", (sub,)).fetchall()]
        cnt = {r["grp"]: r["n"] for r in conn.execute(
            "SELECT grp, COUNT(*) AS n FROM member_watch WHERE sub=? GROUP BY grp", (sub,)).fetchall()}
    return [{"name": g, "count": cnt.get(g, 0)} for g in gs]


def items_of(sub: str, grp: str):
    ensure_tables()
    with _store()._conn() as conn:
        rows = conn.execute("SELECT code, name, market, added_at FROM member_watch WHERE sub=? AND grp=? ORDER BY added_at",
                            (sub, grp)).fetchall()
    return [dict(r) for r in rows]


def add_item(sub: str, grp: str, code: str, name: str, market: str) -> str:
    """回傳 ok／group_missing／full。"""
    ensure_tables()
    with _store()._conn() as conn:
        if not conn.execute("SELECT 1 FROM member_groups WHERE sub=? AND grp=?", (sub, grp)).fetchone():
            return "group_missing"
        n = conn.execute("SELECT COUNT(*) AS n FROM member_watch WHERE sub=? AND grp=?", (sub, grp)).fetchone()["n"]
        have = conn.execute("SELECT 1 FROM member_watch WHERE sub=? AND grp=? AND code=?", (sub, grp, code)).fetchone()
        if not have and n >= MAX_PER_GROUP:
            return "full"
        conn.execute("INSERT INTO member_watch (sub, grp, code, name, market, added_at) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (sub, grp, code) DO NOTHING", (sub, grp, code, name, market, _now()))
    return "ok"


def remove_item(sub: str, grp: str, code: str):
    ensure_tables()
    with _store()._conn() as conn:
        conn.execute("DELETE FROM member_watch WHERE sub=? AND grp=? AND code=?", (sub, grp, code))


def add_group(sub: str, name: str) -> str:
    ensure_tables()
    with _store()._conn() as conn:
        gs = conn.execute("SELECT COUNT(*) AS n FROM member_groups WHERE sub=?", (sub,)).fetchone()["n"]
        if gs >= MAX_GROUPS:
            return "full"
        if conn.execute("SELECT 1 FROM member_groups WHERE sub=? AND grp=?", (sub, name)).fetchone():
            return "exists"
        conn.execute("INSERT INTO member_groups (sub, grp, sort_no) VALUES (?, ?, ?)", (sub, name, gs))
    return "ok"


def remove_group(sub: str, name: str) -> str:
    ensure_tables()
    with _store()._conn() as conn:
        gs = conn.execute("SELECT COUNT(*) AS n FROM member_groups WHERE sub=?", (sub,)).fetchone()["n"]
        if gs <= 1:
            return "last"
        conn.execute("DELETE FROM member_watch WHERE sub=? AND grp=?", (sub, name))
        conn.execute("DELETE FROM member_groups WHERE sub=? AND grp=?", (sub, name))
    return "ok"


# ───────── 登入狀態 ─────────
def current_member():
    p = unsign(request.cookies.get(COOKIE, ""))
    return get_member(p["sub"]) if p and p.get("sub") else None


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not enabled():
            return jsonify({"error": "尚未啟用會員登入"}), 503
        m = current_member()
        if not m:
            return jsonify({"error": "請先登入", "login": True}), 401
        if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-TS") != "1":
            return jsonify({"error": "請求來源不正確"}), 403
        request.member = m
        return f(*a, **k)
    return w


def _set_cookie(resp, name, value, ttl):
    resp.set_cookie(name, value, max_age=ttl, httponly=True, secure=True, samesite="Lax", path="/")


# ───────── 登入牆（類似 TradingView：登入後才能使用站內功能） ─────────
_OPEN_PATHS = ("/api/me", "/healthz", "/health", "/auth/")


def gate():
    """before_request：登入功能已啟用時，/api/* 需 Google 登入（或後台管理員已登入）才能存取。
    登入功能未設定時完全不鎖；環境變數 REQUIRE_LOGIN=0 可暫時關閉登入牆。"""
    try:
        if not enabled() or os.environ.get("REQUIRE_LOGIN", "1").strip() == "0":
            return None
        p = request.path
        if not p.startswith("/api/") or p.startswith(_OPEN_PATHS):
            return None
        if current_member():
            return None
        try:
            import admin_auth
            if admin_auth.REQUIRED and admin_auth.authed():   # 後台密碼有設定、且已登入後台才放行
                return None
        except Exception:
            pass
        r = jsonify({"error": "請先使用 Google 登入", "login_required": True})
        r.status_code = 401
        r.headers["Cache-Control"] = "no-store"
        return r
    except Exception as e:
        logger.warning(f"gate: {e}")
        return None


# ───────── 路由：登入 ─────────
@bp.route("/api/me")
def api_me():
    if not enabled():
        return jsonify({"login_enabled": False, "user": None})
    m = None
    try:
        m = current_member()
    except Exception as e:
        logger.warning(f"/api/me: {e}")
    return jsonify({"login_enabled": True, "user": ({"name": m.get("name") or m.get("email"), "email": m.get("email")} if m else None)})


@bp.route("/auth/google")
def auth_google():
    if not enabled():
        return redirect("/?login=disabled")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    q = urllib.parse.urlencode({
        "client_id": client_id(), "redirect_uri": _redirect_uri(), "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account"})
    resp = redirect(GOOGLE_AUTH + "?" + q)
    _set_cookie(resp, OAUTH_COOKIE, sign({"s": state, "n": nonce, "exp": int(time.time()) + OAUTH_TTL}), OAUTH_TTL)
    return resp


def verify_id_token(idt: str, nonce: str, now=None):
    """直接從 Google token 端點拿到的 id_token：檢查 iss／aud／exp／nonce／email_verified，回傳 claims 或 None。"""
    try:
        claims = json.loads(_unb64(idt.split(".")[1]))
    except Exception:
        return None
    now = now or time.time()
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return None
    if claims.get("aud") != client_id() or claims.get("exp", 0) <= now:
        return None
    if claims.get("nonce") != nonce or not claims.get("sub"):
        return None
    if claims.get("email_verified") is False:
        return None
    return claims


@bp.route("/auth/google/callback")
def auth_callback():
    if not enabled():
        return redirect("/?login=disabled")
    ck = unsign(request.cookies.get(OAUTH_COOKIE, ""))
    state, code = request.args.get("state", ""), request.args.get("code", "")
    if request.args.get("error") or not ck or not code or not hmac.compare_digest(str(ck.get("s", "")), state):
        return redirect("/?login=failed")
    try:
        r = requests.post(GOOGLE_TOKEN, data={
            "code": code, "client_id": client_id(), "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"}, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Google token HTTP {r.status_code}: {r.text[:200]}")
            return redirect("/?login=failed")
        claims = verify_id_token(r.json().get("id_token", ""), ck.get("n"))
        if not claims:
            return redirect("/?login=failed")
        sub = str(claims["sub"])
        upsert_member(sub, claims.get("email", ""), (claims.get("name") or "")[:40])
    except Exception as e:
        logger.warning(f"auth_callback: {e}")
        return redirect("/?login=failed")
    resp = redirect("/?login=ok")
    _set_cookie(resp, COOKIE, sign({"sub": sub, "exp": int(time.time()) + SESSION_TTL}), SESSION_TTL)
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    return resp


@bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ───────── 路由：自選股 ─────────
def _clean_group(v) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[<>&\"'\\]", "", str(v or ""))).strip()[:12]


def _flags_for(code, market, fx, black):
    out = []
    if code in (fx.get("halt") or {}):
        out.append("暫停交易")
    if code in (fx.get("altered") or []):
        out.append("變更交易")
    if code in black or code in (fx.get("otc_disposal") or {}) or code in (fx.get("otc_attention") or {}):
        out.append("處置／注意")
    if code in (fx.get("margin_stop") or {}):
        out.append("停資停券")
    return out


def build_rows(items):
    """把清單代號補上即時報價、訊號、旗標。報價用 build_live_universe（盤中為官方即時快照）。"""
    from stock_universe import build_live_universe, BLACKLIST_CODES
    uni = {s["code"]: s for s in build_live_universe()}
    try:
        import market_extras
        fx = market_extras.get_flags()
    except Exception:
        fx = {}
    sigs = {}
    try:
        for p in _store().get_recent_signals(limit=80, days_back=30):
            if p.get("result") == "pending" and p.get("status") == "active":
                sigs[str(p.get("ticker", "")).split(".")[0]] = p
    except Exception:
        pass
    rows = []
    for it in items:
        c = it["code"]
        u = uni.get(c) or {}
        sg = sigs.get(c)
        price, chg = u.get("close"), u.get("change_pct")
        prev = (price / (1 + chg / 100)) if price and chg is not None else None
        rows.append({
            "code": c, "name": u.get("name") or it.get("name"), "market": u.get("market") or it.get("market"),
            "sector": u.get("sector"), "price": price,
            "change": round(price - prev, 2) if prev else None, "change_pct": chg,
            "volume_lots": u.get("volume_lots"), "live": bool(u.get("live")), "quote_date": u.get("quote_date"),
            "signal": ({"direction": sg.get("direction"), "score": sg.get("score")} if sg else None),
            "flags": _flags_for(c, u.get("market"), fx, BLACKLIST_CODES),
            "listed": bool(u),
        })
    return rows


@bp.route("/api/my/watchlist", methods=["GET"])
@login_required
def my_watch_get():
    sub = request.member["sub"]
    gs = groups_of(sub)
    grp = _clean_group(request.args.get("grp")) or (gs[0]["name"] if gs else DEFAULT_GROUP)
    if grp not in [g["name"] for g in gs]:
        grp = gs[0]["name"] if gs else DEFAULT_GROUP
    return jsonify({"groups": gs, "grp": grp, "items": build_rows(items_of(sub, grp))})


@bp.route("/api/my/watchlist", methods=["POST"])
@login_required
def my_watch_add():
    from stock_universe import build_full_universe
    j = request.get_json(silent=True) or {}
    code = str(j.get("code", "")).strip().upper()
    grp = _clean_group(j.get("grp")) or DEFAULT_GROUP
    if not _CODE_RE.match(code):
        return jsonify({"error": "代號格式不正確"}), 400
    s = next((x for x in build_full_universe() if x.get("code") == code), None)
    if not s:
        return jsonify({"error": "查無此股票代號"}), 404
    r = add_item(request.member["sub"], grp, code, s.get("name", ""), s.get("market", ""))
    if r == "group_missing":
        return jsonify({"error": "找不到這個清單"}), 404
    if r == "full":
        return jsonify({"error": f"每個清單最多 {MAX_PER_GROUP} 檔"}), 400
    return jsonify({"ok": True})


@bp.route("/api/my/watchlist", methods=["DELETE"])
@login_required
def my_watch_remove():
    code = str(request.args.get("code", "")).strip().upper()
    grp = _clean_group(request.args.get("grp")) or DEFAULT_GROUP
    if not _CODE_RE.match(code):
        return jsonify({"error": "代號格式不正確"}), 400
    remove_item(request.member["sub"], grp, code)
    return jsonify({"ok": True})


@bp.route("/api/my/groups", methods=["POST"])
@login_required
def my_group_add():
    name = _clean_group((request.get_json(silent=True) or {}).get("name"))
    if not name:
        return jsonify({"error": "請輸入清單名稱"}), 400
    r = add_group(request.member["sub"], name)
    if r == "full":
        return jsonify({"error": f"最多 {MAX_GROUPS} 個清單"}), 400
    if r == "exists":
        return jsonify({"error": "已有同名清單"}), 400
    return jsonify({"ok": True})


@bp.route("/api/my/groups", methods=["DELETE"])
@login_required
def my_group_remove():
    r = remove_group(request.member["sub"], _clean_group(request.args.get("name")))
    if r == "last":
        return jsonify({"error": "至少要保留一個清單"}), 400
    return jsonify({"ok": True})
