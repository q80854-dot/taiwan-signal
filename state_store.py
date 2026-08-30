"""
state_store.py — SQLite 資料庫管理 v1.0
儲存：訊號記錄、績效統計、掃描歷史、meta 資料
"""
import sqlite3, json, logging, os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Any
from config import SYSTEM

logger = logging.getLogger(__name__)
DB_PATH = SYSTEM["db_path"]


class StateStore:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id            TEXT PRIMARY KEY,
                    ticker        TEXT, code TEXT, name TEXT, sector TEXT,
                    direction     TEXT, score REAL, grade TEXT,
                    current_price REAL, entry_price REAL,
                    stop_loss     REAL, tp1 REAL, tp2 REAL, tp3 REAL,
                    sl_pct        REAL, tp1_pct REAL, tp2_pct REAL,
                    -- ★ 修正：2026-08-30（第二輪）——這個欄位名字是舊制（1000股=1張）留下來的，
                    -- 但 signal_engine.calc_position_size() 改用零股(股數)為單位後，這裡實際存的
                    -- 是股數，不是張數。沒有重新命名欄位是為了不動到既有 production DB 的 schema
                    -- （SQLite ALTER COLUMN 風險/複雜度不成比例），純粹是欄位名稱歷史遺留，
                    -- 讀寫這個欄位時請當作股數處理，不要照字面當成張數。
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
            """)
        logger.info(f"資料庫初始化：{DB_PATH}")

    # ── 訊號 ──
    def save_signal(self, sig: Dict) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO signals (
                        id,ticker,code,name,sector,direction,score,grade,
                        current_price,entry_price,stop_loss,tp1,tp2,tp3,
                        sl_pct,tp1_pct,tp2_pct,suggested_lots,risk_twd,risk_pct,
                        position_value,roundtrip_cost,vol_ratio,adx_value,rsi_value,
                        inst_signal,weekly_bias,reason_brief,reason_full,
                        result,close_price,pnl_twd,status,generated_at,raw_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
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

    def get_pending_signals(self) -> List[Dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE status='active' AND result='pending' ORDER BY generated_at DESC"
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
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key,value,updated_at) VALUES(?,?,?)",
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
