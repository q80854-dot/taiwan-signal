"""問題回報：會員回報錯誤／資料問題／功能建議，站主在後台查看與標記處理狀態。

- 送出：POST /api/my/feedback（需登入＋X-TS 標頭；每人每小時最多 5 則；內容長度有上限）
- 我的回報：GET /api/my/feedback
- 站主：GET /api/admin/feedback、POST /api/admin/feedback/status
- 新回報會用 Telegram 通知站主（只帶類別與前 200 字，不帶 Email）
回報內容一律當純文字保存，網頁顯示時跳脫，不會被當成 HTML 執行。
"""
import json
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import members

logger = logging.getLogger(__name__)
bp = Blueprint("feedback", __name__)

CATEGORIES = {"bug": "錯誤回報", "data": "資料不正確", "idea": "功能建議", "other": "其他"}
STATUSES = {"open": "待處理", "doing": "處理中", "done": "已處理", "wontfix": "不處理"}
MAX_MSG = 2000
MIN_MSG = 5
PER_HOUR = 5
_ready = {"ok": False}
_PAGE_RE = re.compile(r"^[a-z]{1,30}$")


def ensure_table():
    if _ready["ok"]:
        return
    with members._store()._conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY, at TEXT, sub TEXT, email TEXT, name TEXT, category TEXT,
                message TEXT, page TEXT, ctx TEXT, status TEXT, note TEXT, updated_at TEXT
            );
        """)
    _ready["ok"] = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_ctx(c) -> str:
    c = c if isinstance(c, dict) else {}
    out = {"ua": str(c.get("ua", ""))[:200], "version": str(c.get("version", ""))[:20],
           "theme": str(c.get("theme", ""))[:10], "mode": str(c.get("mode", ""))[:10]}
    for k in ("vw", "vh"):
        try:
            out[k] = max(0, min(10000, int(c.get(k) or 0)))
        except (TypeError, ValueError):
            out[k] = 0
    return json.dumps(out, ensure_ascii=False)


def _count_recent(sub: str) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with members._store()._conn() as conn:
        r = conn.execute("SELECT COUNT(*) AS n FROM feedback WHERE sub=? AND at>=?", (sub, since)).fetchone()
    return int((dict(r) if r else {}).get("n") or 0)


def _notify(cat: str, name: str, page: str, msg: str):
    def run():
        try:
            from telegram_bot import send_alert
            send_alert(f"📝 新的問題回報（{CATEGORIES.get(cat, cat)}）\n來自：{name or '會員'}　頁面：{page or '—'}\n{msg[:200]}", "info")
        except Exception as e:
            logger.warning(f"feedback notify: {e}")
    threading.Thread(target=run, daemon=True).start()


@bp.route("/api/my/feedback", methods=["POST"])
@members.login_required
def submit():
    ensure_table()
    j = request.get_json(silent=True) or {}
    cat = str(j.get("category", "")).strip()
    msg = str(j.get("message", "")).replace("\r", "").strip()
    page = str(j.get("page", "")).strip()
    if cat not in CATEGORIES:
        return jsonify({"error": "請選擇回報類別"}), 400
    if len(msg) < MIN_MSG:
        return jsonify({"error": f"請至少描述 {MIN_MSG} 個字"}), 400
    if len(msg) > MAX_MSG:
        return jsonify({"error": f"內容最多 {MAX_MSG} 字"}), 400
    if page and not _PAGE_RE.match(page):
        page = ""
    m = request.member
    if _count_recent(m["sub"]) >= PER_HOUR:
        return jsonify({"error": "回報太頻繁，請一小時後再試"}), 429
    fid = secrets.token_hex(8)
    with members._store()._conn() as conn:
        conn.execute("INSERT INTO feedback (id, at, sub, email, name, category, message, page, ctx, status, note, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (fid, _now(), m["sub"], m.get("email", ""), m.get("name", ""), cat, msg, page,
                      _clean_ctx(j.get("ctx")), "open", "", _now()))
    _notify(cat, m.get("name", ""), page, msg)
    return jsonify({"ok": True, "id": fid})


def _row(r, admin: bool) -> dict:
    d = dict(r)
    out = {"id": d["id"], "at": d["at"], "category": d["category"], "category_zh": CATEGORIES.get(d["category"], d["category"]),
           "message": d["message"], "page": d["page"], "status": d["status"],
           "status_zh": STATUSES.get(d["status"], d["status"]), "note": d.get("note") or "", "updated_at": d.get("updated_at")}
    if admin:
        out.update({"name": d.get("name"), "email": d.get("email")})
        try:
            out["ctx"] = json.loads(d.get("ctx") or "{}")
        except Exception:
            out["ctx"] = {}
    return out


@bp.route("/api/my/feedback", methods=["GET"])
@members.login_required
def mine():
    ensure_table()
    with members._store()._conn() as conn:
        rows = conn.execute("SELECT * FROM feedback WHERE sub=? ORDER BY at DESC LIMIT 20", (request.member["sub"],)).fetchall()
    return jsonify({"items": [_row(r, False) for r in rows], "categories": CATEGORIES})


def _owner_only():
    if not members.viewer_is_owner():
        return members._forbid()
    return None


@bp.route("/api/admin/feedback", methods=["GET"])
def admin_list():
    if (r := _owner_only()) is not None:
        return r
    ensure_table()
    st = request.args.get("status", "")
    with members._store()._conn() as conn:
        if st in STATUSES:
            rows = conn.execute("SELECT * FROM feedback WHERE status=? ORDER BY at DESC LIMIT 200", (st,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM feedback ORDER BY at DESC LIMIT 200").fetchall()
        counts = {k: 0 for k in STATUSES}
        for c in conn.execute("SELECT status, COUNT(*) AS n FROM feedback GROUP BY status").fetchall():
            c = dict(c)
            counts[c["status"]] = int(c["n"])
    return jsonify({"items": [_row(r, True) for r in rows], "counts": counts, "statuses": STATUSES})


@bp.route("/api/admin/feedback/status", methods=["POST"])
def admin_status():
    if (r := _owner_only()) is not None:
        return r
    if request.headers.get("X-TS") != "1":
        return jsonify({"error": "請求來源不正確"}), 403
    ensure_table()
    j = request.get_json(silent=True) or {}
    fid, st, note = str(j.get("id", "")), str(j.get("status", "")), str(j.get("note", ""))[:500]
    if st not in STATUSES or not re.fullmatch(r"[0-9a-f]{16}", fid):
        return jsonify({"error": "參數不正確"}), 400
    with members._store()._conn() as conn:
        conn.execute("UPDATE feedback SET status=?, note=?, updated_at=? WHERE id=?", (st, note, _now(), fid))
    return jsonify({"ok": True})
