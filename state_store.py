"""
state_store.py — 資料庫管理 v2.0（SQLite / Postgres 雙後端）
儲存：訊號記錄、績效統計、掃描歷史、meta 資料

★ 修正：2026-09-16——使用者要求核對歷年訊號時發現：Render 上這個 Web Service
沒有掛 Persistent Disk，SQLite 檔案路徑（instance/twstock.db）是容器本機的
暫存空間，每次重新部署（本專案至今已部署15次以上）容器都會重建、SQLite檔案
就會被清空歸零。這代表 get_performance_summary() 的勝率/已平倉筆數統計，
事實上從系統上線以來從未真正累積過——不是策略打不贏，是這個統計數字實際上
幾乎沒被量測過。使用者選擇方案B：改用 Render Postgres（真正的持久化資料庫，
不隨部署清空、不受容器重建影響）。

做法：有設定 DATABASE_URL 環境變數（Render Postgres 會自動提供）就走 Postgres，
沒有設定（例如本機開發、或環境變數還沒接上）則自動退回原本的 SQLite 行為，
兩種模式下對外的方法簽章、回傳格式完全一致，app.py/scanner.py 等呼叫端
不需要跟著修改。
"""
import sqlite3, json, logging, os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any
from config import SYSTEM

logger = logging.getLogger(__name__)
DB_PATH = SYSTEM["db_path"]

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    # Render/一般雲端供應商常給 "postgres://" 開頭的連線字串，
    # psycopg2 本身可以接受，但保留正規化以避免未來換成需要 "postgresql://" 的工具時出錯。
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


class _CursorShim:
    """讓呼叫端可以繼續用 conn.execute(...)（沿用 SQLite 的寫法），
    內部依後端轉成 psycopg2 的 cursor.execute(...)，並把 '?' 佔位符轉成 '%s'。"""
    def __init__(self, raw, is_pg: bool):
        self._raw = raw          # sqlite3.Connection 或 psycopg2 cursor
        self._pg = is_pg

    def execute(self, sql: str, params: tuple = ()):
        if self._pg:
            self._raw.execute(sql.replace("?", "%s"), params)
            return self._raw
        return self._raw.execute(sql, params)

    def executescript(self, sql: str):
        # psycopg2 cursor.execute 可以一次送出多個以 ; 分隔的 DDL 陳述句，不需要 executescript
        if self._pg:
            self._raw.execute(sql)
        else:
            self._raw.executescript(sql)


class StateStore:
    def __init__(self):
        if not USE_PG:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        if USE_PG:
            raw_conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            cur = raw_conn.cursor()
            try:
                yield _CursorShim(cur, True)
                raw_conn.commit()
            except Exception:
                raw_conn.rollback()
                raise
            finally:
                cur.close()
                raw_conn.close()
        else:
            raw_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            raw_conn.row_factory = sqlite3.Row
            try:
                with raw_conn:
                    yield _CursorShim(raw_conn, False)
            finally:
                raw_conn.close()

    def _init_db(self):
        if USE_PG:
            ddl = """
                CREATE TABLE IF NOT EXISTS signals (
                    id            TEXT PRIMARY KEY,
                    ticker        TEXT, code TEXT, name TEXT, sector TEXT,
                    direction     TEXT, score REAL, grade TEXT,
                    current_price REAL, entry_price REAL,
                    stop_loss     REAL, tp1 REAL, tp2 REAL, tp3 REAL,
                    sl_pct        REAL, tp1_pct REAL, tp2_pct REAL,
                    suggested_lots INTEGER, risk_twd REAL, risk_pct REAL,
                    position_value REAL, roundtrip_cost REAL,
                    vol_ratio     REAL, adx_value REAL, rsi_value REAL,
                    inst_signal   TEXT, weekly_bias TEXT,
                    reason_brief  TEXT, reason_full TEXT,
                    result        TEXT DEFAULT 'pending',
                    close_price   REAL, pnl_twd REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
                    status        TEXT DEFAULT 'active',
                    generated_at  TEXT, closed_at TEXT, raw_json TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_history (
                    id            SERIAL PRIMARY KEY,
                    scan_date     TEXT, scanned INTEGER, signals_found INTEGER,
                    signals_sent  INTEGER, duration_min REAL, errors INTEGER, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_ticker  ON signals(ticker);
                CREATE INDEX IF NOT EXISTS idx_signals_date    ON signals(generated_at);
                CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
            """
        else:
            ddl = """
                CREATE TABLE IF NOT EXISTS signals (
                    id            TEXT PRIMARY KEY,
                    ticker        TEXT, code TEXT, name TEXT, sector TEXT,
                    direction     TEXT, score REAL, grade TEXT,
                    current_price REAL, entry_price REAL,
                    stop_loss     REAL, tp1 REAL, tp2 REAL, tp3 REAL,
                    sl_pct        REAL, tp1_pct REAL, tp2_pct REAL,
                    suggested_lots INTEGER, risk_twd REAL, risk_pct REAL,
                    position_value REAL, roundtrip_cost REAL,
                    vol_ratio     REAL, adx_value REAL, rsi_value REAL,
                    inst_signal   TEXT, weekly_bias TEXT,
                    reason_brief  TEXT, reason_full TEXT,
                    result        TEXT DEFAULT 'pending',
                    close_price   REAL, pnl_twd REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
                    status        TEXT DEFAULT 'active',
                    generated_at  TEXT, closed_at TEXT, raw_json TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_history (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_date     TEXT, scanned INTEGER, signals_found INTEGER,
                    signals_sent  INTEGER, duration_min REAL, errors INTEGER, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_ticker  ON signals(ticker);
                CREATE INDEX IF NOT EXISTS idx_signals_date    ON signals(generated_at);
                CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
            """
        with self._conn() as conn:
            conn.executescript(ddl)
        logger.info(f"資料庫初始化：{'Postgres（持久化）' if USE_PG else DB_PATH}")

    # ── 訊號 ──
    def save_signal(self, sig: Dict) -> bool:
        try:
            cols = ("id,ticker,code,name,sector,direction,score,grade,"
                    "current_price,entry_price,stop_loss,tp1,tp2,tp3,"
                    "sl_pct,tp1_pct,tp2_pct,suggested_lots,risk_twd,risk_pct,"
                    "position_value,roundtrip_cost,vol_ratio,adx_value,rsi_value,"
                    "inst_signal,weekly_bias,reason_brief,reason_full,"
                    "result,close_price,pnl_twd,status,generated_at,raw_json")
            col_list = cols.split(",")
            if USE_PG:
                update_clause = ",".join(f"{c}=EXCLUDED.{c}" for c in col_list if c != "id")
                sql = (f"INSERT INTO signals ({cols}) VALUES ({','.join(['?']*len(col_list))}) "
                       f"ON CONFLICT (id) DO UPDATE SET {update_clause}")
            else:
                sql = f"INSERT OR REPLACE INTO signals ({cols}) VALUES ({','.join(['?']*len(col_list))})"
            with self._conn() as conn:
                conn.execute(sql, (
                    sig["id"],sig["ticker"],sig.get("code",""),sig["name"],sig.get("sector",""),
                    sig["direction"],sig["score"],sig.get("grade",""),
                    sig["current_price"],sig["entry_price"],sig["stop_loss"],
                    sig["tp1"],sig.get("tp2",0),sig.get("tp3",0),
                    sig.get("sl_pct",0),sig.get("tp1_pct",0),sig.get("tp2_pct",0),
                    sig.get("suggested_shares",sig.get("suggested_lots",1)),sig.get("risk_twd",0),sig.get("risk_pct",0),
                    sig.get("position_value",0),sig.get("roundtrip_cost",0),
                    sig.get("vol_ratio",1),sig.get("adx_value",0),sig.get("rsi_value",50),
                    sig.get("inst_signal",""),sig.get("weekly_bias",""),
                    sig.get("reason_brief",""),sig.get("reason_full",""),
                    sig.get("result","pending"),sig.get("close_price"),
                    sig.get("pnl_twd",0),sig.get("status","active"),
                    sig.get("generated_at",datetime.now(timezone.utc).isoformat()),
                    json.dumps(sig,ensure_ascii=False),
                ))
            return True
        except Exception as e:
            logger.error(f"save_signal {sig.get('id')}: {e}"); return False

    def update_signal_result(self, sig_id: str, result: str,
                              close_price: float, pnl_twd: float, pnl_pct: float = 0):
        try:
            with self._conn() as conn:
                conn.execute("""
                    UPDATE signals SET result=?,close_price=?,pnl_twd=?,pnl_pct=?,
                    status='closed',closed_at=? WHERE id=?
                """, (result,close_price,pnl_twd,pnl_pct,datetime.now(timezone.utc).isoformat(),sig_id))
        except Exception as e: logger.error(f"update_signal_result: {e}")

    def get_recent_signals(self, limit: int = 20, days_back: int = 30) -> List[Dict]:
        try:
            cutoff = (datetime.now(timezone.utc)-timedelta(days=days_back)).isoformat()
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE generated_at>=? ORDER BY generated_at DESC LIMIT ?",
                    (cutoff,limit)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"get_recent_signals: {e}"); return []

    def get_pending_signals(self, limit: int = 500) -> List[Dict]:
        # ★ 修正：2026-09-16——原本這裡沒有 LIMIT，理論上會隨著 expire_days 從未真正
        # 被執行（見 scanner._resolve_pending_signals 新增的逾期強制平倉邏輯）而無限增長，
        # 每次掃描前的結算階段就要逐筆重新抓歷史K棒判斷停損停利，耗時隨筆數線性增加。
        # 現在 scanner.py 已經會主動把超過 signal_expire_days 天還沒觸價的訊號強制平倉，
        # 這裡的 LIMIT 是第二層保險，避免萬一結算邏輯本身出錯時筆數還是失控增長。
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE status='active' AND result='pending' "
                    "ORDER BY generated_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"get_pending_signals: {e}")
            return []

    # ── 績效 ──
    def get_performance_summary(self) -> Dict:
        try:
            with self._conn() as conn:
                row = conn.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN result IN ('tp1','tp2','tp3') THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN result='sl' THEN 1 ELSE 0 END) as losses,
                           SUM(pnl_twd) as total_pnl,
                           AVG(CASE WHEN result LIKE 'tp%' THEN pnl_twd ELSE NULL END) as avg_win,
                           AVG(CASE WHEN result='sl' THEN pnl_twd ELSE NULL END) as avg_loss
                    FROM signals WHERE status='closed'
                """).fetchone()
            total=row["total"] or 0; wins=row["wins"] or 0; losses=row["losses"] or 0
            closed=wins+losses
            return {
                "total":      total, "closed": closed, "pending": total-closed,
                "wins":       wins,  "losses": losses,
                "win_rate":   round(wins/max(closed,1)*100,1),
                "total_pnl":  round(row["total_pnl"] or 0,0),
                "avg_win":    round(row["avg_win"]  or 0,0),
                "avg_loss":   round(row["avg_loss"] or 0,0),
                "recent_trades": self.get_recent_signals(20),
            }
        except Exception as e:
            logger.error(f"get_performance_summary: {e}")
            return {"total":0,"wins":0,"losses":0,"win_rate":0,"total_pnl":0,"recent_trades":[]}

    # ── 掃描歷史 ──
    def save_scan_history(self, stats: Dict):
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT INTO scan_history
                    (scan_date,scanned,signals_found,signals_sent,duration_min,errors,created_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (datetime.now().strftime("%Y-%m-%d"),stats.get("scanned",0),
                      stats.get("signals_found",0),stats.get("signals_sent",0),
                      stats.get("duration_min",0),stats.get("errors",0),
                      datetime.now(timezone.utc).isoformat()))
        except Exception as e: logger.error(f"save_scan_history: {e}")

    # ── Meta ──
    def get_meta(self, key: str, default: Any = None) -> Any:
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default
        except Exception as e:
            logger.warning(f"get_meta({key}): {e}")
            return default

    def set_meta(self, key: str, value: Any):
        try:
            if USE_PG:
                sql = ("INSERT INTO meta(key,value,updated_at) VALUES(?,?,?) "
                       "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at")
            else:
                sql = "INSERT OR REPLACE INTO meta(key,value,updated_at) VALUES(?,?,?)"
            with self._conn() as conn:
                conn.execute(sql,
                    (key, json.dumps(value,ensure_ascii=False), datetime.now(timezone.utc).isoformat())
                )
        except Exception as e: logger.error(f"set_meta: {e}")

    # ── 連虧計數 ──
    def get_consec_loss(self, symbol: str) -> int:
        return self.get_meta(f"consec_loss_{symbol}", 0)

    def set_consec_loss(self, symbol: str, count: int):
        self.set_meta(f"consec_loss_{symbol}", count)

    # ── 資金曲線 ──
    def append_equity(self, balance: float):
        curve = self.get_meta("equity_curve", [])
        curve.append({"ts": datetime.now(timezone.utc).isoformat(), "balance": balance})
        if len(curve) > 500: curve = curve[-500:]
        self.set_meta("equity_curve", curve)

    def get_equity_curve(self) -> List[Dict]:
        return self.get_meta("equity_curve", [])


store = StateStore()
