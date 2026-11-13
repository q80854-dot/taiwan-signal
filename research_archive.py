"""研究用官方資料每日快照（2026-11-13，使用者核准）。

為什麼：證交所、櫃買中心、集保的公開 API 多半只給「當天／當週」的資料（停牌、處置、注意股、全額交割、
產業別、股權分散……），沒有歷史。歷史若要回補需付費；但只要每天盤後原樣存一份，從今天起就不會再缺。
（與影子追蹤同一個道理：過去缺的買不回來，未來不再缺。）

原則：
  · 原樣保存（gzip＋base64 文字欄位，跨 SQLite／Postgres 一致），並記錄網址、抓取時間、SHA-256、筆數。
  · 只存「內容有變」的版本：同一來源與上一份 SHA-256 相同就不重存（週更的集保資料每天抓也只存一份）。
  · 抓不到、被擋、不是 JSON／CSV 的，記一列 status='error'（不存內容、不算成功），不猜、不補。
  · 純研究：不被任何訊號、評分、風控讀取；寫入獨立表 research_snapshots。
"""
import base64
import gzip
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
_TPE = timezone(timedelta(hours=8))
_ready = False

_TWSE = "https://openapi.twse.com.tw/v1/"
_TPEX = "https://www.tpex.org.tw/openapi/v1/"
# (來源名稱, 網址, 格式, 說明)
SOURCES = [
    ("twse_halt", _TWSE + "exchangeReport/TWTAWU", "json", "上市 暫停交易證券"),
    ("twse_altered", _TWSE + "exchangeReport/TWT85U", "json", "上市 變更交易（分盤／全額交割）"),
    ("twse_margin_stop", _TWSE + "exchangeReport/BFI84U", "json", "上市 停資停券預告"),
    ("twse_disposal", _TWSE + "announcement/punish", "json", "上市 處置股票"),
    ("twse_notice", _TWSE + "announcement/notice", "json", "上市 注意股票"),
    ("twse_notice_count", _TWSE + "announcement/notetrans", "json", "上市 注意累計可能達處置標準"),
    ("twse_exright_preview", _TWSE + "exchangeReport/TWT48U_ALL", "json", "上市 除權除息預告"),
    ("twse_company", _TWSE + "opendata/t187ap03_L", "json", "上市公司基本資料（含產業別）"),
    ("tpex_disposal", _TPEX + "tpex_disposal_information", "json", "上櫃 處置有價證券"),
    ("tpex_notice", _TPEX + "tpex_trading_warning_information", "json", "上櫃 注意股票"),
    ("tpex_cmode", _TPEX + "tpex_cmode", "json", "上櫃 變更交易／分盤／管理股票／停止交易"),
    ("tpex_halt_today", _TPEX + "tpex_spendi_today", "json", "上櫃 當日暫停／恢復交易"),
    ("tpex_company", _TPEX + "mopsfin_t187ap03_O", "json", "上櫃公司基本資料（含產業別）"),
    ("tdcc_holding_dist", "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", "csv", "集保 股權分散表（週）"),
]


def _ddl():
    from state_store import USE_PG
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return f"""
    CREATE TABLE IF NOT EXISTS research_snapshots (
        id {pk}, snap_date TEXT, source TEXT, url TEXT, fetched_at TEXT, status TEXT,
        sha256 TEXT, n_rows INTEGER, bytes INTEGER, content_gz_b64 TEXT, error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rsnap_src ON research_snapshots(source, snap_date);
    """


def ensure_table():
    global _ready
    if _ready:
        return
    from state_store import store
    with store._conn() as conn:
        conn.executescript(_ddl())
    _ready = True


def _count_rows(body: bytes, fmt: str):
    """驗證格式並回傳筆數；格式不對拋例外（被擋時常回 HTML）。"""
    if fmt == "json":
        d = json.loads(body.decode("utf-8-sig"))
        if not isinstance(d, (list, dict)):
            raise ValueError("JSON 結構不符")
        return len(d) if isinstance(d, list) else len(d.get("data") or [])
    text = body.decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise ValueError("回傳 HTML，不是 CSV")
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2 or "," not in lines[0]:
        raise ValueError("CSV 結構不符")
    return len(lines) - 1


def _fetch(url: str) -> bytes:
    import requests
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    r.raise_for_status()
    return r.content


def snapshot(now=None, fetch=None) -> dict:
    """抓一輪所有來源並存檔。回傳 {saved, unchanged, errors, sources}。fetch 可注入（測試用）。"""
    ensure_table()
    from state_store import store
    fetch = fetch or _fetch
    now = now or datetime.now(_TPE)
    day = now.strftime("%Y-%m-%d")
    out = {"date": day, "saved": 0, "unchanged": 0, "errors": 0, "sources": {}}
    for name, url, fmt, _desc in SOURCES:
        rec = {"snap_date": day, "source": name, "url": url, "fetched_at": now.isoformat()}
        try:
            body = fetch(url)
            n = _count_rows(body, fmt)
            sha = hashlib.sha256(body).hexdigest()
            with store._conn() as conn:
                last = conn.execute(
                    "SELECT sha256 FROM research_snapshots WHERE source=? AND status='ok' ORDER BY id DESC LIMIT 1",
                    (name,)).fetchone()
            if last and last["sha256"] == sha:
                out["unchanged"] += 1
                out["sources"][name] = "unchanged"
                continue
            rec.update(status="ok", sha256=sha, n_rows=n, bytes=len(body),
                       content_gz_b64=base64.b64encode(gzip.compress(body)).decode("ascii"), error=None)
            out["saved"] += 1
            out["sources"][name] = f"saved:{n}"
        except Exception as e:
            rec.update(status="error", sha256=None, n_rows=None, bytes=None, content_gz_b64=None, error=str(e)[:300])
            out["errors"] += 1
            out["sources"][name] = "error"
        cols = ("snap_date", "source", "url", "fetched_at", "status", "sha256", "n_rows", "bytes",
                "content_gz_b64", "error")
        with store._conn() as conn:
            conn.execute(f"INSERT INTO research_snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         tuple(rec.get(c) for c in cols))
    try:
        store.set_meta("research_snapshot_date", day)
    except Exception:
        pass
    return out


def done_today(now=None) -> bool:
    try:
        from state_store import store
        day = (now or datetime.now(_TPE)).strftime("%Y-%m-%d")
        return store.get_meta("research_snapshot_date") == day
    except Exception:
        return False


def index(limit=500) -> list:
    """快照清單（不含內容）。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT id, snap_date, source, url, fetched_at, status, sha256, n_rows, bytes, error "
            "FROM research_snapshots ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def content(snap_id: int):
    """回傳 (原始 bytes, 來源名稱)；找不到或非成功列回 (None, None)。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        r = conn.execute("SELECT source, status, content_gz_b64 FROM research_snapshots WHERE id=?",
                         (int(snap_id),)).fetchone()
    if not r or r["status"] != "ok" or not r["content_gz_b64"]:
        return None, None
    return gzip.decompress(base64.b64decode(r["content_gz_b64"])), r["source"]
