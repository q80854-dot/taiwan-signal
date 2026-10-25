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
from datetime import datetime, timedelta, timezone
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
MAX_ALERTS = 100            # 每位會員最多設定的到價提醒檔數
MAX_NOTES = 100             # 每位會員保留的站內通知則數（超過自動清掉最舊的）
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
        CREATE TABLE IF NOT EXISTS member_status (
            sub TEXT PRIMARY KEY, disabled INTEGER DEFAULT 0, sess_ver INTEGER DEFAULT 0, login_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS login_events (
            id TEXT PRIMARY KEY, at TEXT, sub TEXT, email TEXT, ok INTEGER, reason TEXT, ip_hash TEXT, ua TEXT
        );
        CREATE TABLE IF NOT EXISTS member_prefs (
            sub TEXT PRIMARY KEY, prefs TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS member_alerts (
            sub TEXT, code TEXT, name TEXT, market TEXT, target REAL, stop REAL,
            enabled INTEGER DEFAULT 1, created_at TEXT, fired TEXT, PRIMARY KEY (sub, code)
        );
        CREATE TABLE IF NOT EXISTS member_notes (
            id TEXT PRIMARY KEY, sub TEXT, at TEXT, kind TEXT, title TEXT, body TEXT, code TEXT, read INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_member_notes_sub ON member_notes(sub, at);
    """
    with _store()._conn() as conn:
        conn.executescript(ddl)
    # 2026-10-20：暫時停權（自動到期）新增欄位；既有資料表用 ALTER 補上，重複執行時忽略「已存在」。
    # ★ 2026-10-24 根因修正：這段 ALTER 原本和上面的 CREATE 共用同一個交易。Postgres 一旦有
    # 一條陳述句出錯，整個交易就會進入 aborted 狀態，commit 時等同全部回滾——所以每次部署後
    # ALTER 因「欄位已存在」而失敗，就把上面新加的 member_prefs／member_alerts／member_notes
    # 的 CREATE 一起回滾掉，導致 production（Postgres）一直說這些表不存在、API 噴 UndefinedTable。
    # （SQLite 不會這樣中止整個交易，所以本機測試過得了、沒抓到。）改成放在獨立交易：即使這條
    # ALTER 失敗也只回滾它自己，不影響上面的建表。
    try:
        with _store()._conn() as conn:
            conn.execute("ALTER TABLE member_status ADD COLUMN disabled_until TEXT")
    except Exception:
        pass
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
        conn.execute("INSERT INTO member_status (sub, disabled, sess_ver, login_count) VALUES (?, 0, 0, 1) "
                     "ON CONFLICT (sub) DO UPDATE SET login_count=member_status.login_count+1", (sub,))
    invalidate_member(sub)


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


# ───────── 會員個人偏好（雲端化：跨裝置一致）─────────
# 使用者要求「提升會員的功能和設定可調整的東西」。原本看盤樣式／排序／主題只存在瀏覽器
# localStorage，換手機或換電腦就不見。這裡把少量個人偏好存進帳號，前端登入後讀回、變更
# 時寫回（未登入仍用 localStorage）。只收白名單鍵、值做型別與長度清洗，避免被塞任意資料。
_PREF_KEYS = {"view", "sort", "sort_dir", "theme", "default_grp", "mobile", "density"}
_PREF_MAX_BYTES = 4000


def _sanitize_prefs(raw: dict) -> dict:
    out = {}
    for k in _PREF_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if k == "sort" and isinstance(v, dict):          # {k: 欄位, d: 1/-1}
            col = re.sub(r"[^a-zA-Z0-9_]", "", str(v.get("k", "")))[:24]
            if col:
                out["sort"] = {"k": col, "d": -1 if v.get("d") in (-1, "-1", "desc") else 1}
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        else:
            out[k] = str(v)[:40]                          # 字串一律截斷，避免過長
    return out


def get_prefs(sub: str) -> dict:
    ensure_tables()
    with _store()._conn() as conn:
        r = conn.execute("SELECT prefs FROM member_prefs WHERE sub=?", (sub,)).fetchone()
    if not r or not r["prefs"]:
        return {}
    try:
        return _sanitize_prefs(json.loads(r["prefs"]) or {})
    except Exception:
        return {}


def set_prefs(sub: str, patch: dict) -> dict:
    """把 patch 合併進既有偏好（只收白名單鍵），回傳清洗後的完整偏好。"""
    ensure_tables()
    merged = {**get_prefs(sub), **_sanitize_prefs(patch or {})}
    raw = json.dumps(merged, ensure_ascii=False)[:_PREF_MAX_BYTES]
    with _store()._conn() as conn:
        conn.execute("INSERT INTO member_prefs (sub, prefs, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT (sub) DO UPDATE SET prefs=EXCLUDED.prefs, updated_at=EXCLUDED.updated_at",
                     (sub, raw, _now()))
    return merged


# ───────── 會員到價提醒 ＋ 站內通知 ─────────
# 使用者要求「提升會員的功能」。會員替自選股設定目標價／停損價，盤中到價時自動寫一則
# 站內通知（會員收不到 Telegram，那是站主專用）。同方向每天最多提醒一次（fired 記當天日期）。
def _f(v):
    """把價格字串/數字轉成正數 float；空值或非正數回 None（不猜、不接受 0 或負數）。"""
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return x if x > 0 else None
    except Exception:
        return None


def alerts_of(sub: str) -> list:
    ensure_tables()
    with _store()._conn() as conn:
        rows = conn.execute("SELECT code, name, market, target, stop, enabled, fired FROM member_alerts "
                            "WHERE sub=? ORDER BY code", (sub,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled", 1))
        try:
            d["fired"] = json.loads(d["fired"]) if d.get("fired") else {}
        except Exception:
            d["fired"] = {}
        out.append(d)
    return out


def set_alert(sub: str, code: str, name: str, market: str, target, stop) -> str:
    """設定/更新一檔到價提醒。回傳 ok／need_price／full。target 或 stop 至少要有一個。"""
    ensure_tables()
    t, s = _f(target), _f(stop)
    if t is None and s is None:
        return "need_price"
    with _store()._conn() as conn:
        exists = conn.execute("SELECT 1 FROM member_alerts WHERE sub=? AND code=?", (sub, code)).fetchone()
        if not exists:
            n = conn.execute("SELECT COUNT(*) AS n FROM member_alerts WHERE sub=?", (sub,)).fetchone()["n"]
            if n >= MAX_ALERTS:
                return "full"
        # 設定變更時清掉 fired 紀錄，讓新的門檻能重新觸發
        conn.execute("INSERT INTO member_alerts (sub, code, name, market, target, stop, enabled, created_at, fired) "
                     "VALUES (?, ?, ?, ?, ?, ?, 1, ?, '{}') "
                     "ON CONFLICT (sub, code) DO UPDATE SET name=EXCLUDED.name, market=EXCLUDED.market, "
                     "target=EXCLUDED.target, stop=EXCLUDED.stop, enabled=1, fired='{}'",
                     (sub, code, name, market, t, s, _now()))
    return "ok"


def remove_alert(sub: str, code: str):
    ensure_tables()
    with _store()._conn() as conn:
        conn.execute("DELETE FROM member_alerts WHERE sub=? AND code=?", (sub, code))


def add_note(sub: str, kind: str, title: str, body: str, code: str = "") -> str:
    """寫一則會員站內通知，並把該會員超過 MAX_NOTES 的最舊通知清掉。回傳 note id。"""
    ensure_tables()
    nid = secrets.token_hex(8)
    with _store()._conn() as conn:
        conn.execute("INSERT INTO member_notes (id, sub, at, kind, title, body, code, read) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                     (nid, sub, _now(), str(kind)[:20], str(title)[:120], str(body)[:400], str(code)[:12]))
        conn.execute("DELETE FROM member_notes WHERE sub=? AND id NOT IN "
                     "(SELECT id FROM member_notes WHERE sub=? ORDER BY at DESC LIMIT ?)",
                     (sub, sub, MAX_NOTES))
    return nid


def notes_of(sub: str, limit: int = 50, unread_only: bool = False) -> list:
    ensure_tables()
    q = "SELECT id, at, kind, title, body, code, read FROM member_notes WHERE sub=?"
    if unread_only:
        q += " AND read=0"
    q += " ORDER BY at DESC LIMIT ?"
    with _store()._conn() as conn:
        rows = conn.execute(q, (sub, int(limit))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["read"] = bool(d.get("read", 0))
        out.append(d)
    return out


def unread_count(sub: str) -> int:
    ensure_tables()
    with _store()._conn() as conn:
        r = conn.execute("SELECT COUNT(*) AS n FROM member_notes WHERE sub=? AND read=0", (sub,)).fetchone()
    return int(r["n"]) if r else 0


def mark_notes_read(sub: str, ids=None) -> int:
    ensure_tables()
    with _store()._conn() as conn:
        if ids:
            ids = [str(x) for x in ids][:200]
            ph = ",".join("?" for _ in ids)
            cur = conn.execute(f"UPDATE member_notes SET read=1 WHERE sub=? AND id IN ({ph})", (sub, *ids))
        else:
            cur = conn.execute("UPDATE member_notes SET read=1 WHERE sub=? AND read=0", (sub,))
        return cur.rowcount if cur.rowcount is not None else 0


def check_member_alerts() -> int:
    """盤中檢查所有會員到價提醒，到價就寫站內通知（同方向每天最多一次）。回傳發出的通知數。
    由排程在交易時段呼叫；用證交所官方即時行情（與自選股顯示同一來源）。"""
    ensure_tables()
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    with _store()._conn() as conn:
        rows = conn.execute("SELECT sub, code, name, market, target, stop, fired FROM member_alerts WHERE enabled=1").fetchall()
    if not rows:
        return 0
    rows = [dict(r) for r in rows]
    pairs = {(r["code"], (r["market"] or "TSE")) for r in rows}
    try:
        import realtime
        quotes = realtime.fetch_mis(list(pairs)) or {}
    except Exception as e:
        logger.warning(f"check_member_alerts: 取即時價失敗 {e}")
        return 0
    sent = 0
    for r in rows:
        q = quotes.get(r["code"]) or {}
        price = q.get("price")
        if not price:
            continue
        try:
            fired = json.loads(r["fired"]) if r.get("fired") else {}
        except Exception:
            fired = {}
        name = r.get("name") or r["code"]
        hit = None
        if r.get("target") and price >= r["target"] and fired.get("target") != today:
            hit = "target"
            add_note(r["sub"], "price_up", f"🔔 {name}（{r['code']}）到目標價",
                     f"現價 {price} 已達你設定的目標價 {r['target']}。", r["code"])
        elif r.get("stop") and price <= r["stop"] and fired.get("stop") != today:
            hit = "stop"
            add_note(r["sub"], "price_down", f"⚠️ {name}（{r['code']}）跌破停損價",
                     f"現價 {price} 已跌破你設定的停損價 {r['stop']}。", r["code"])
        if hit:
            fired[hit] = today
            with _store()._conn() as conn:
                conn.execute("UPDATE member_alerts SET fired=? WHERE sub=? AND code=?",
                             (json.dumps(fired), r["sub"], r["code"]))
            sent += 1
    if sent:
        logger.info(f"check_member_alerts: 發出 {sent} 則會員到價通知")
    return sent


# ───────── 登入狀態 ─────────
def status_of(sub: str) -> dict:
    ensure_tables()
    with _store()._conn() as conn:
        r = conn.execute("SELECT disabled, sess_ver, login_count, disabled_until FROM member_status WHERE sub=?", (sub,)).fetchone()
    return dict(r) if r else {"disabled": 0, "sess_ver": 0, "login_count": 0, "disabled_until": None}


def _suspended(st: dict) -> bool:
    """暫時停權是否仍在效期內（到期即自動失效，不需手動解除）。"""
    u = st.get("disabled_until")
    if not u:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(str(u).replace("Z", "+00:00"))
    except Exception:
        return False


def effective_disabled(st: dict) -> bool:
    """實際是否被擋：永久停權(disabled=1) 或 暫時停權仍在效期內。"""
    return bool(st.get("disabled")) or _suspended(st)


def current_member():
    """登入中的會員；cookie 簽名不對、已過期、被停權、或已被「登出全部裝置」作廢，都視為未登入。"""
    p = unsign(request.cookies.get(COOKIE, ""))
    if not p or not p.get("sub"):
        return None
    m, st = _member_cached(p["sub"])
    if not m:
        return None
    if effective_disabled(st) or int(p.get("v", 0)) != int(st.get("sess_ver") or 0):
        return None
    return m


_MCACHE: dict = {}      # sub -> (到期時間, 會員, 狀態)；單一 worker，所以停權／登出全部裝置時直接清掉即可立刻生效


def _member_cached(sub: str):
    """每個請求都要驗身分，原本每次連兩次資料庫；湧入時會被拖慢，改成記憶體短暫快取（15 秒）。"""
    now = time.time()
    ent = _MCACHE.get(sub)
    if ent and ent[0] > now:
        return ent[1], ent[2]
    m = get_member(sub)
    st = status_of(sub) if m else {}
    if len(_MCACHE) > 3000:
        _MCACHE.clear()
    _MCACHE[sub] = (now + 15, m, st)
    return m, st


def invalidate_member(sub: str = ""):
    if sub:
        _MCACHE.pop(sub, None)
    else:
        _MCACHE.clear()


# ───────── 登入稽核與頻率限制 ─────────
_AUTH_RATE: dict = {}


def _ip() -> str:
    return (request.headers.get("CF-Connecting-IP") or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "?")


def _ip_hash() -> str:
    """只存雜湊後的前 16 碼，不保存原始 IP（隱私）；同一個 IP 仍能辨識是否反覆嘗試。"""
    return hmac.new(_key(), _ip().encode(), hashlib.sha256).hexdigest()[:16]


def auth_rate_ok(limit: int = 30, window: int = 600) -> bool:
    """登入入口與回呼的每 IP 頻率限制（預設 10 分鐘 30 次），避免被拿來灌請求或暴力嘗試。"""
    now = time.time()
    k = _ip()
    q = [t for t in _AUTH_RATE.get(k, []) if now - t < window]
    if len(_AUTH_RATE) > 5000:
        _AUTH_RATE.clear()
    if len(q) >= limit:
        _AUTH_RATE[k] = q
        return False
    q.append(now)
    _AUTH_RATE[k] = q
    return True


def record_login(sub: str, email: str, ok: bool, reason: str = ""):
    try:
        ensure_tables()
        with _store()._conn() as conn:
            conn.execute("INSERT INTO login_events (id, at, sub, email, ok, reason, ip_hash, ua) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (secrets.token_hex(8), _now(), sub or "", email or "", 1 if ok else 0, reason[:40],
                          _ip_hash(), (request.headers.get("User-Agent") or "")[:120]))
            if secrets.randbelow(50) == 0:      # 偶爾清掉 90 天前的紀錄
                cut = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
                conn.execute("DELETE FROM login_events WHERE at < ?", (cut,))
    except Exception as e:
        logger.warning(f"record_login: {e}")


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


def owner_required(f):
    """★ 2026-10-15 資安稽核 L2/L4：站主專屬端點的縱深防禦，不只依賴 before_request 的
    集中登入牆。登入啟用時非站主一律 403；登入未啟用＝單人模式，視為站主放行
    （與 viewer_is_owner 一致）。"""
    @wraps(f)
    def w(*a, **k):
        if not viewer_is_owner():
            return _forbid()
        return f(*a, **k)
    return w


def _set_cookie(resp, name, value, ttl):
    resp.set_cookie(name, value, max_age=ttl, httponly=True, secure=True, samesite="Lax", path="/")


# ───────── 登入牆（類似 TradingView：登入後才能使用站內功能） ─────────
_OPEN_PATHS = ("/api/me", "/healthz", "/health", "/auth/", "/api/public/")
_OWNER_ONLY_GET = ("/api/health", "/api/settings", "/api/audit", "/api/diagnostics", "/api/admin/",
                   "/api/backtest", "/api/backfill", "/api/learning")
DEFAULT_OWNER = "q80854@gmail.com,q80855@gmail.com"   # 2026-10-08：加入帳號擁有者 q80854；Render 若設了 OWNER_EMAILS 以其為準


def owner_emails() -> set:
    raw = os.environ.get("OWNER_EMAILS", "").strip() or DEFAULT_OWNER
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_owner(m) -> bool:
    return bool(m) and (m.get("email") or "").strip().lower() in owner_emails()


def _forbid():
    r = jsonify({"error": "此功能僅限站主使用", "forbidden": True})
    r.status_code = 403
    r.headers["Cache-Control"] = "no-store"
    return r


def gate():
    """before_request：登入功能已啟用時，/api/* 需 Google 登入（或後台管理員已登入）才能存取。
    登入功能未設定時完全不鎖；環境變數 REQUIRE_LOGIN=0 可暫時關閉登入牆。"""
    try:
        if not enabled():
            return None
        p = request.path
        # CSRF 第二道防線：會改變資料的請求若帶有 Origin，必須與本站相同（SameSite=Lax 之外再擋一層）
        if request.method not in ("GET", "HEAD", "OPTIONS") and (p.startswith("/api/") or p.startswith("/auth/")):
            og = request.headers.get("Origin")
            if og and urllib.parse.urlparse(og).netloc != request.host:
                return _forbid()
        if os.environ.get("REQUIRE_LOGIN", "1").strip() == "0":
            return None
        if not p.startswith("/api/") or p.startswith(_OPEN_PATHS):
            return None
        m = current_member()
        if m:
            if is_owner(m):
                return None
            # 一般會員（任何 Google 帳號都能註冊）：只能讀資料與管理自己的自選股，
            # 不能觸發掃描／回測／回填、不能改共用設定或看後台資料。
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if not (p.startswith("/api/my/") or p == "/api/me"):
                    return _forbid()
            elif p.startswith(_OWNER_ONLY_GET):
                return _forbid()
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


def viewer_is_owner() -> bool:
    """目前這個請求是不是站主（登入功能沒開＝單人使用，視為站主）。"""
    try:
        if not enabled() or os.environ.get("REQUIRE_LOGIN", "1").strip() == "0":
            return True
        return is_owner(current_member())
    except Exception:
        return False


# 一般會員（外部測試者）看不到會透露站主本金的欄位：建議張數／股數、風險金額、損益金額
_PRIVATE_KEYS = {"suggested_lots", "suggested_shares", "risk_twd", "pnl_twd", "total_risk_twd", "loss_twd",
                 "lots", "shares", "account_balance"}
_PRIVATE_PATHS = ("/api/state", "/api/signals", "/api/positions", "/api/autopsy", "/api/performance",
                  "/api/instruments/", "/api/events", "/api/signal_check/")


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in _PRIVATE_KEYS}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


def scrub_private(resp):
    """after_request：非站主的回應移除本金相關欄位。須在 protect.install 之後註冊（先於快取寫入執行）。"""
    try:
        if (request.path.startswith(_PRIVATE_PATHS) and resp.status_code == 200
                and (resp.content_type or "").startswith("application/json") and not viewer_is_owner()):
            data = json.loads(resp.get_data(as_text=True))
            resp.set_data(json.dumps(_strip(data), ensure_ascii=False))
            resp.headers["Content-Length"] = str(len(resp.get_data()))
    except Exception as e:
        logger.warning(f"scrub_private: {e}")
    return resp


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
    return jsonify({"login_enabled": True, "user": ({"name": m.get("name") or m.get("email"), "email": m.get("email"), "owner": is_owner(m)} if m else None)})


@bp.route("/auth/google")
def auth_google():
    if not enabled():
        return redirect("/?login=disabled")
    if not auth_rate_ok():
        return redirect("/?login=busy")
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
    if not auth_rate_ok():
        return redirect("/?login=busy")
    ck = unsign(request.cookies.get(OAUTH_COOKIE, ""))
    state, code = request.args.get("state", ""), request.args.get("code", "")
    if request.args.get("error") or not ck or not code or not hmac.compare_digest(str(ck.get("s", "")), state):
        record_login("", "", False, "state_or_error")
        return redirect("/?login=failed")
    try:
        r = requests.post(GOOGLE_TOKEN, data={
            "code": code, "client_id": client_id(), "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(), "grant_type": "authorization_code"}, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Google token HTTP {r.status_code}: {r.text[:200]}")
            record_login("", "", False, f"token_http_{r.status_code}")
            return redirect("/?login=failed")
        claims = verify_id_token(r.json().get("id_token", ""), ck.get("n"))
        if not claims:
            record_login("", "", False, "bad_id_token")
            return redirect("/?login=failed")
        sub = str(claims["sub"])
        if status_of(sub).get("disabled"):
            record_login(sub, claims.get("email", ""), False, "disabled")
            return redirect("/?login=banned")
        upsert_member(sub, claims.get("email", ""), (claims.get("name") or "")[:40])
        record_login(sub, claims.get("email", ""), True, "ok")
    except Exception as e:
        logger.warning(f"auth_callback: {e}")
        return redirect("/?login=failed")
    resp = redirect("/?login=ok")
    _set_cookie(resp, COOKIE, sign({"sub": sub, "exp": int(time.time()) + SESSION_TTL, "v": int(status_of(sub).get("sess_ver") or 0)}), SESSION_TTL)
    resp.delete_cookie(OAUTH_COOKIE, path="/")
    return resp


@bp.route("/auth/logout", methods=["POST"])
def auth_logout():
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@bp.route("/auth/logout_all", methods=["POST"])
def auth_logout_all():
    """登出所有裝置：把會員的工作階段版本 +1，之前發出的所有登入 cookie 立刻失效。"""
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    m = current_member()
    if m:
        with _store()._conn() as conn:
            conn.execute("INSERT INTO member_status (sub, disabled, sess_ver, login_count) VALUES (?, 0, 1, 0) "
                         "ON CONFLICT (sub) DO UPDATE SET sess_ver=member_status.sess_ver+1", (m["sub"],))
        invalidate_member(m["sub"])
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ───────── 路由：站主後台（會員統計） ─────────
def _tw_date(iso: str) -> str:
    try:
        return (datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _tw_hour(iso: str):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).hour
    except Exception:
        return None


def member_stats() -> dict:
    ensure_tables()
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    N_SERIES = 60                                             # 時間序列拉長到 60 天（登入紀錄保留 90 天，資料足夠）
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(N_SERIES - 1, -1, -1)]
    d7 = (now - timedelta(days=6)).strftime("%Y-%m-%d")       # 近 7 天（含今天）
    d30 = (now - timedelta(days=29)).strftime("%Y-%m-%d")     # 近 30 天（含今天）
    with _store()._conn() as conn:
        ms = [dict(r) for r in conn.execute("SELECT sub, email, name, created_at, last_login FROM members").fetchall()]
        st = {r["sub"]: dict(r) for r in conn.execute("SELECT sub, disabled, login_count, disabled_until FROM member_status").fetchall()}
        wc = {r["sub"]: r["n"] for r in conn.execute("SELECT sub, COUNT(*) AS n FROM member_watch GROUP BY sub").fetchall()}
        gc = {r["sub"]: r["n"] for r in conn.execute("SELECT sub, COUNT(*) AS n FROM member_groups GROUP BY sub").fetchall()}
        # 熱門自選股：被最多「不同會員」加入的代號前 20（名稱取任一筆既有名稱）
        pop = [{"code": r["code"], "name": r["name"] or "", "members": r["n"]}
               for r in conn.execute(
                   "SELECT code, MAX(name) AS name, COUNT(DISTINCT sub) AS n FROM member_watch "
                   "GROUP BY code ORDER BY n DESC, code LIMIT 20").fetchall()]
        cut = (datetime.now(timezone.utc) - timedelta(days=N_SERIES)).isoformat()
        ev = [dict(r) for r in conn.execute("SELECT at, sub, email, ok, reason, ip_hash, ua FROM login_events WHERE at >= ? ORDER BY at DESC", (cut,)).fetchall()]
    for m in ms:
        m["created_day"], m["last_day"] = _tw_date(m.get("created_at") or ""), _tw_date(m.get("last_login") or "")
    ok_by_day, fail_by_day = {d: 0 for d in days}, {d: 0 for d in days}
    new_by_day = {d: 0 for d in days}
    for m in ms:
        if m["created_day"] in new_by_day:
            new_by_day[m["created_day"]] += 1
    hours = [0] * 24
    fail_reasons: dict = {}
    dev = {"mobile": 0, "desktop": 0}
    for e in ev:
        d = _tw_date(e["at"])
        if d in ok_by_day:
            (ok_by_day if e["ok"] else fail_by_day)[d] += 1
        if e["ok"]:
            h = _tw_hour(e["at"])
            if h is not None:
                hours[h] += 1
            dev["mobile" if re.search(r"Mobi|Android|iPhone|iPad", e.get("ua") or "") else "desktop"] += 1
        else:
            r = e.get("reason") or "未註明"
            fail_reasons[r] = fail_reasons.get(r, 0) + 1
    fails24 = sum(1 for e in ev if not e["ok"] and e["at"] >= (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
    rows = [{"name": m.get("name") or "", "email": m.get("email") or "", "created": m["created_day"], "last_login": m["last_day"],
             "logins": (st.get(m["sub"]) or {}).get("login_count", 0), "watch": wc.get(m["sub"], 0), "groups": gc.get(m["sub"], 0),
             "disabled": bool((st.get(m["sub"]) or {}).get("disabled")),
             "suspended": _suspended(st.get(m["sub"]) or {}),
             "suspended_until": _tw_date((st.get(m["sub"]) or {}).get("disabled_until") or "") if _suspended(st.get(m["sub"]) or {}) else None,
             "sub": m["sub"], "owner": is_owner(m)}
            for m in sorted(ms, key=lambda x: x.get("last_login") or "", reverse=True)]
    n_watchers = len(wc)
    return {
        "total": len(ms), "disabled": sum(1 for r in rows if r["disabled"]),
        "new_today": sum(1 for m in ms if m["created_day"] == today), "new_7d": sum(1 for m in ms if m["created_day"] >= d7),
        "new_30d": sum(1 for m in ms if m["created_day"] >= d30),
        "active_today": sum(1 for m in ms if m["last_day"] == today), "active_7d": sum(1 for m in ms if m["last_day"] >= d7),
        "active_30d": sum(1 for m in ms if m["last_day"] >= d30),
        "watch_total": sum(wc.values()), "fails_24h": fails24,
        # 自選股使用分佈
        "watch_users": n_watchers, "watch_none": len(ms) - n_watchers,
        "watch_avg": round(sum(wc.values()) / n_watchers, 1) if n_watchers else 0,
        "watch_max": max(wc.values()) if wc else 0, "groups_total": sum(gc.values()),
        "popular_watch": pop,
        "login_hours": hours,                                  # 24 格：各時段（台北）成功登入次數
        "fail_reasons": sorted(({"reason": k, "n": v} for k, v in fail_reasons.items()), key=lambda x: -x["n"]),
        "devices": dev,                                        # 近 60 天成功登入的裝置分佈（粗分手機／桌機）
        "series": [{"d": d, "ok": ok_by_day[d], "fail": fail_by_day[d], "new": new_by_day[d]} for d in days],
        "members": rows,
        "events": [{"at": e["at"], "email": e["email"], "ok": bool(e["ok"]), "reason": e["reason"], "ip": e["ip_hash"][:8], "ua": e["ua"][:60]} for e in ev[:120]],
    }


@bp.route("/api/admin/members", methods=["GET"])
def admin_members():
    m = current_member()
    if not is_owner(m):
        return _forbid()
    return jsonify(member_stats())


@bp.route("/api/admin/members/status", methods=["POST"])
def admin_member_status():
    m = current_member()
    if not is_owner(m):
        return _forbid()
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    d = request.get_json(silent=True) or {}
    sub, dis = str(d.get("sub") or ""), 1 if d.get("disabled") else 0
    target = get_member(sub)
    if not target:
        return jsonify({"error": "找不到會員"}), 404
    if is_owner(target):
        return jsonify({"error": "不能停權站主帳號"}), 400
    # 恢復（disabled=0）時一併清掉暫時停權的到期時間，避免「解除了永久停權卻還卡在暫時停權」。
    with _store()._conn() as conn:
        conn.execute("INSERT INTO member_status (sub, disabled, sess_ver, login_count, disabled_until) VALUES (?, ?, 0, 0, NULL) "
                     "ON CONFLICT (sub) DO UPDATE SET disabled=EXCLUDED.disabled" + ("" if dis else ", disabled_until=NULL"), (sub, dis))
    invalidate_member(sub)
    return jsonify({"ok": True, "disabled": bool(dis)})


@bp.route("/api/admin/members/suspend", methods=["POST"])
def admin_member_suspend():
    """暫時停權：設定天數，到期自動恢復（不需手動解除）。期間內無法登入、現有登入立即失效。"""
    m = current_member()
    if not is_owner(m):
        return _forbid()
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    d = request.get_json(silent=True) or {}
    sub = str(d.get("sub") or "")
    try:
        days = int(d.get("days") or 0)
    except (TypeError, ValueError):
        days = 0
    if not (1 <= days <= 365):
        return jsonify({"error": "天數需在 1～365 之間"}), 400
    target = get_member(sub)
    if not target:
        return jsonify({"error": "找不到會員"}), 404
    if is_owner(target):
        return jsonify({"error": "不能停權站主帳號"}), 400
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with _store()._conn() as conn:
        conn.execute("INSERT INTO member_status (sub, disabled, sess_ver, login_count, disabled_until) VALUES (?, 0, 0, 0, ?) "
                     "ON CONFLICT (sub) DO UPDATE SET disabled_until=EXCLUDED.disabled_until", (sub, until))
    invalidate_member(sub)
    return jsonify({"ok": True, "suspended_until": until, "days": days})


@bp.route("/api/admin/members/logout", methods=["POST"])
def admin_member_logout():
    """強制登出（不封鎖）：把該會員所有裝置踢出、需重新登入，但帳號仍可用。"""
    m = current_member()
    if not is_owner(m):
        return _forbid()
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    sub = str((request.get_json(silent=True) or {}).get("sub") or "")
    if not get_member(sub):
        return jsonify({"error": "找不到會員"}), 404
    with _store()._conn() as conn:
        conn.execute("INSERT INTO member_status (sub, disabled, sess_ver, login_count) VALUES (?, 0, 1, 0) "
                     "ON CONFLICT (sub) DO UPDATE SET sess_ver=member_status.sess_ver+1", (sub,))
    invalidate_member(sub)
    return jsonify({"ok": True})


@bp.route("/api/admin/members/delete", methods=["POST"])
def admin_member_delete():
    """永久刪除會員：清除會員身分、自選股、分組與其登入紀錄（不可復原）。站主帳號不可刪。"""
    m = current_member()
    if not is_owner(m):
        return _forbid()
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    sub = str((request.get_json(silent=True) or {}).get("sub") or "")
    target = get_member(sub)
    if not target:
        return jsonify({"error": "找不到會員"}), 404
    if is_owner(target):
        return jsonify({"error": "不能刪除站主帳號"}), 400
    with _store()._conn() as conn:
        for tbl in ("member_watch", "member_groups", "member_status", "member_prefs", "member_alerts", "member_notes", "login_events", "members"):
            conn.execute(f"DELETE FROM {tbl} WHERE sub=?", (sub,))
    invalidate_member(sub)
    return jsonify({"ok": True})


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


def build_rows(items, view="quote"):
    """把清單代號補上即時報價、訊號、旗標。報價用 build_live_universe（盤中為官方即時快照）。

    view 控制「看盤樣式」要帶哪些額外欄位（像三竹可切換報價／基本／籌碼）：
      - quote（預設）：只有報價＋訊號＋旗標，不額外抓任何資料（零延遲）。
      - basic：加本益比／殖利率／淨值比，取自 fetch_valuation_map()（全市場一次、已快取）。
      - chips：加「今日三大法人買賣超（張）」，取自 fetch_institutional_flow()（全市場一次、已快取）。
    兩個額外來源都是整個市場一張快取表、每檔只做字典查詢，不逐檔對外抓取，所以切換樣式不會變慢；
    抓不到就留 None（前端顯示「—」），不以推估或舊值充數。"""
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
    vm = {}
    if view == "basic":
        try:
            from fundamentals import fetch_valuation_map
            vm = fetch_valuation_map() or {}
        except Exception:
            vm = {}
    inst = {}
    if view == "chips":
        try:
            from data_fetcher import fetch_institutional_flow
            inst = fetch_institutional_flow() or {}
        except Exception:
            inst = {}
    rows = []
    for it in items:
        c = it["code"]
        u = uni.get(c) or {}
        sg = sigs.get(c)
        price, chg = u.get("close"), u.get("change_pct")
        prev = (price / (1 + chg / 100)) if price and chg is not None else None
        row = {
            "code": c, "name": u.get("name") or it.get("name"), "market": u.get("market") or it.get("market"),
            "sector": u.get("sector"), "price": price,
            "change": round(price - prev, 2) if prev else None, "change_pct": chg,
            "volume_lots": u.get("volume_lots"), "live": bool(u.get("live")), "quote_date": u.get("quote_date"),
            "signal": ({"direction": sg.get("direction"), "score": sg.get("score")} if sg else None),
            "flags": _flags_for(c, u.get("market"), fx, BLACKLIST_CODES),
            "listed": bool(u),
        }
        if view == "basic":
            v = vm.get(c) or {}
            row.update({"pe": v.get("pe"), "yield_pct": v.get("yield_pct"), "pb": v.get("pb")})
        elif view == "chips":
            q = inst.get(c) or {}
            row.update({"inst_foreign": q.get("foreign_net"), "inst_trust": q.get("trust_net"),
                        "inst_total": q.get("total_net")})
        rows.append(row)
    _overlay_fresh_quotes(rows)
    return rows


def _overlay_fresh_quotes(rows):
    """★ 修正：2026-10-23——使用者回報「自選股漲跌跟個股研究不一樣（大盤跌、自選股卻顯示上漲）」。
    根因：build_live_universe() 的官方日行情（尤其上市）常晚一天更新，自選股仍顯示前一交易日的
    收盤與漲跌幅；個股研究頁則會改用證交所 MIS 的最新成交（app.py api_instrument 同一套邏輯）。
    這裡一次批次抓清單內所有代號的 MIS 報價，資料日期比官方日行情新才覆蓋（收盤後即當日定案價），
    抓不到就維持官方數字，不估算。"""
    if not rows:
        return
    try:
        import realtime
        got = realtime.fetch_mis([(r["code"], r.get("market")) for r in rows if r.get("code")])
    except Exception as e:
        logger.warning(f"watchlist MIS overlay 失敗：{e}")
        return
    today = realtime.now_tpe().strftime("%Y-%m-%d")
    for r in rows:
        q = got.get(str(r.get("code") or "").upper())
        if not q or not q.get("price") or not q.get("date"):
            continue
        if q["date"] > (r.get("quote_date") or ""):
            r.update({"price": q["price"], "change": q.get("change"), "change_pct": q.get("change_pct"),
                      "quote_date": q["date"], "quote_source": q.get("source"),
                      "live": bool(q["date"] == today and realtime.market_open())})
            if q.get("volume_lots") is not None:
                r["volume_lots"] = q["volume_lots"]


@bp.route("/api/my/watchlist", methods=["GET"])
@login_required
def my_watch_get():
    sub = request.member["sub"]
    gs = groups_of(sub)
    grp = _clean_group(request.args.get("grp")) or (gs[0]["name"] if gs else DEFAULT_GROUP)
    if grp not in [g["name"] for g in gs]:
        grp = gs[0]["name"] if gs else DEFAULT_GROUP
    view = (request.args.get("view") or "quote").strip().lower()
    if view not in ("quote", "basic", "chips"):
        view = "quote"
    return jsonify({"groups": gs, "grp": grp, "view": view, "items": build_rows(items_of(sub, grp), view)})


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


@bp.route("/api/my/prefs", methods=["GET"])
@login_required
def my_prefs_get():
    return jsonify({"prefs": get_prefs(request.member["sub"])})


@bp.route("/api/my/prefs", methods=["PUT", "POST"])
@login_required
def my_prefs_set():
    j = request.get_json(silent=True) or {}
    if "prefs" in j:                       # {"prefs": {...}} 形式：prefs 必須是物件
        patch = j["prefs"]
    else:                                   # 也接受直接把偏好當 body（{...}）
        patch = j
    if not isinstance(patch, dict):
        return jsonify({"error": "格式不正確"}), 400
    return jsonify({"prefs": set_prefs(request.member["sub"], patch)})


@bp.route("/api/my/alerts", methods=["GET"])
@login_required
def my_alerts_get():
    return jsonify({"alerts": alerts_of(request.member["sub"]), "max": MAX_ALERTS})


@bp.route("/api/my/alerts", methods=["PUT", "POST"])
@login_required
def my_alerts_set():
    from stock_universe import build_full_universe
    j = request.get_json(silent=True) or {}
    code = str(j.get("code", "")).strip().upper()
    if not _CODE_RE.match(code):
        return jsonify({"error": "代號格式不正確"}), 400
    s = next((x for x in build_full_universe() if x.get("code") == code), None)
    if not s:
        return jsonify({"error": "查無此股票代號"}), 404
    r = set_alert(request.member["sub"], code, s.get("name", ""), s.get("market", ""), j.get("target"), j.get("stop"))
    if r == "need_price":
        return jsonify({"error": "請至少輸入目標價或停損價"}), 400
    if r == "full":
        return jsonify({"error": f"最多設定 {MAX_ALERTS} 檔提醒"}), 400
    return jsonify({"ok": True, "alerts": alerts_of(request.member["sub"])})


@bp.route("/api/my/alerts", methods=["DELETE"])
@login_required
def my_alerts_remove():
    code = str(request.args.get("code", "")).strip().upper()
    if not _CODE_RE.match(code):
        return jsonify({"error": "代號格式不正確"}), 400
    remove_alert(request.member["sub"], code)
    return jsonify({"ok": True})


@bp.route("/api/my/notifications", methods=["GET"])
@login_required
def my_notes_get():
    sub = request.member["sub"]
    return jsonify({"notes": notes_of(sub, limit=int(request.args.get("limit", 50)),
                                      unread_only=request.args.get("unread") == "1"),
                    "unread": unread_count(sub)})


@bp.route("/api/my/notifications/read", methods=["POST"])
@login_required
def my_notes_read():
    j = request.get_json(silent=True) or {}
    ids = j.get("ids") if isinstance(j.get("ids"), list) else None
    n = mark_notes_read(request.member["sub"], ids)
    return jsonify({"ok": True, "marked": n, "unread": unread_count(request.member["sub"])})
