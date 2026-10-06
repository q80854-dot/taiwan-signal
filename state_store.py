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
                CREATE TABLE IF NOT EXISTS ohlcv_bars (
                    ticker TEXT, tf_key TEXT, bar_date TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume BIGINT,
                    updated_at TEXT,
                    PRIMARY KEY (ticker, tf_key, bar_date)
                );
                CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_tf ON ohlcv_bars(ticker, tf_key, bar_date);
                CREATE TABLE IF NOT EXISTS monthly_revenue_history (
                    ticker TEXT, period TEXT, yoy_pct REAL, mom_pct REAL, market TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (ticker, period)
                );
                CREATE INDEX IF NOT EXISTS idx_rev_hist_ticker ON monthly_revenue_history(ticker, period);
                CREATE TABLE IF NOT EXISTS margin_chg_daily_history (
                    bar_date TEXT PRIMARY KEY, balance BIGINT, chg_pct REAL, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS inst_daily (
                    trade_date TEXT, code TEXT, name TEXT,
                    foreign_net INTEGER, trust_net INTEGER, total_net INTEGER,
                    PRIMARY KEY (trade_date, code)
                );
                CREATE INDEX IF NOT EXISTS idx_inst_daily_code ON inst_daily(code, trade_date);
                CREATE TABLE IF NOT EXISTS quarterly_margin_snapshot (
                    code TEXT, period TEXT, gross_margin_pct REAL, operating_margin_pct REAL,
                    updated_at TEXT,
                    PRIMARY KEY (code, period)
                );
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
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id TEXT PRIMARY KEY, tier TEXT, added_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_ticker  ON signals(ticker);
                CREATE INDEX IF NOT EXISTS idx_signals_date    ON signals(generated_at);
                CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
            """
        else:
            ddl = """
                CREATE TABLE IF NOT EXISTS ohlcv_bars (
                    ticker TEXT, tf_key TEXT, bar_date TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                    updated_at TEXT,
                    PRIMARY KEY (ticker, tf_key, bar_date)
                );
                CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_tf ON ohlcv_bars(ticker, tf_key, bar_date);
                CREATE TABLE IF NOT EXISTS monthly_revenue_history (
                    ticker TEXT, period TEXT, yoy_pct REAL, mom_pct REAL, market TEXT,
                    updated_at TEXT,
                    PRIMARY KEY (ticker, period)
                );
                CREATE INDEX IF NOT EXISTS idx_rev_hist_ticker ON monthly_revenue_history(ticker, period);
                CREATE TABLE IF NOT EXISTS margin_chg_daily_history (
                    bar_date TEXT PRIMARY KEY, balance INTEGER, chg_pct REAL, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS inst_daily (
                    trade_date TEXT, code TEXT, name TEXT,
                    foreign_net INTEGER, trust_net INTEGER, total_net INTEGER,
                    PRIMARY KEY (trade_date, code)
                );
                CREATE INDEX IF NOT EXISTS idx_inst_daily_code ON inst_daily(code, trade_date);
                CREATE TABLE IF NOT EXISTS quarterly_margin_snapshot (
                    code TEXT, period TEXT, gross_margin_pct REAL, operating_margin_pct REAL,
                    updated_at TEXT,
                    PRIMARY KEY (code, period)
                );
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
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id TEXT PRIMARY KEY, tier TEXT, added_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signals_ticker  ON signals(ticker);
                CREATE INDEX IF NOT EXISTS idx_signals_date    ON signals(generated_at);
                CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
            """
        with self._conn() as conn:
            conn.executescript(ddl)
        # ★ 新增：2026-10-05——使用者回報「今日訊號 TG 有推、網頁卻什麼都沒有，
        # 觀察清單跟通知也是空的」。根因：(1) 通知只送去 Telegram，網頁端沒有任何
        # 地方記錄過；(2) 觀察清單只存在瀏覽器 localStorage，換裝置/清快取就消失，
        # 後端完全不知道。這裡新增兩張持久化表（Postgres/SQLite 雙後端），讓網頁
        # 通知中心與觀察清單跟 TG 推播共用同一份後端事實來源。
        if USE_PG:
            extra_ddl = """
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY, ts TEXT, kind TEXT, title TEXT, body TEXT, ticker TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker TEXT PRIMARY KEY, code TEXT, name TEXT, note TEXT, added_at TEXT
                );
            """
        else:
            extra_ddl = """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, title TEXT, body TEXT, ticker TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
                CREATE TABLE IF NOT EXISTS watchlist (
                    ticker TEXT PRIMARY KEY, code TEXT, name TEXT, note TEXT, added_at TEXT
                );
            """
        try:
            with self._conn() as conn:
                conn.executescript(extra_ddl)
        except Exception as e:
            logger.warning(f"建立 events/watchlist 表失敗（不影響其餘功能）: {e}")
        # ★ 新增：2026-09-16——回應三方AI交叉比對（Perplexity/ChatGPT/Gemini）中
        # ChatGPT提出的建議：訊號的「參考價位」（產生訊號當下的價格，即現有的
        # entry_price/current_price欄位）跟「使用者實際成交價」目前是同一個概念，
        # 完全沒有欄位可以承載後者，導致B4（滑價未被評估）長期想做的「用真實
        # 成交價校正滑價估計」永遠無法真正開始——這裡加一個新欄位承接使用者
        # 之後透過Telegram /fill 指令回報的實際成交價，現有表格已經在正式環境
        # 運作過、不能用 DROP TABLE 重建，所以用 ALTER TABLE ADD COLUMN 做
        # 遷移，且對「這個欄位是不是已經加過」做防呆（見 _migrate_add_column），
        # 避免每次啟動都重複執行或在欄位已存在時噴錯。
        self._migrate_add_column("signals", "actual_entry_price", "REAL")
        # ★ 新增：2026-09-28——使用者驗證台灣50數據時發現：TWSE 季報 API 的
        # 營收/毛利/營業利益欄位是「累計數」（今年至該季為止的加總），不是
        # 單季數字（拿台積電比對法說會公布的單季毛利率67.7%，系統算出來是
        # 67.03%，量級對得上「累計」而非「單季」）。原本的季度趨勢比對是直接
        # 拿累計數的毛利率相減，Q2 vs Q1 還好，但 Q3 vs Q2 這種比較會被「多
        # 一季累計進去」稀釋，嚴謹度不夠。修正做法：多存「累計原始數字」
        # （營收/毛利/營業利益，不是比率），下次比較時用「這一季累計 減去
        # 上一季累計」反推出真正的單季數字，同年度內quarter==1不用減（本身
        # 就是單季）。這裡用 ALTER TABLE ADD COLUMN 遷移，理由跟上面
        # actual_entry_price 一樣——正式環境已經在跑，不能 DROP TABLE 重建。
        self._migrate_add_column("quarterly_margin_snapshot", "cum_revenue", "REAL")
        self._migrate_add_column("quarterly_margin_snapshot", "cum_gross_profit", "REAL")
        self._migrate_add_column("quarterly_margin_snapshot", "cum_operating_income", "REAL")
        logger.info(f"資料庫初始化：{'Postgres（持久化）' if USE_PG else DB_PATH}")

    def _migrate_add_column(self, table: str, column: str, coltype: str):
        try:
            with self._conn() as conn:
                if USE_PG:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
                else:
                    existing = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    col_names = {row["name"] for row in existing}
                    if column not in col_names:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        except Exception as e:
            logger.warning(f"_migrate_add_column({table}.{column}): {e}")

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

    # ★ 新增：2026-09-29——使用者回報兩個相關 bug：(1) 績效分析頁「總交易 6」
    # 跟下面列出的約19筆對不起來、(2) 交易歷史頁顯示「尚無記錄」但績效頁明明
    # 有交易資料。根因是同一個：get_performance_summary() 的 "total" 是
    # COUNT(*) WHERE status='closed'（只算已平倉），但底下塞給前端表格的
    # "recent_trades" 卻是呼叫 get_recent_signals()，那支函式完全不看 status，
    # 抓的是「近30天所有產生過的訊號」（active／pending／closed 都算在內）
    # ——兩個數字從來就不是同一個母體，對不起來是必然的；而 dashboard.html
    # 的「交易歷史」頁更誇張，直接讀的是 _state.active_signals（今日訊號用的
    # 「目前有效訊號」清單），跟「歷史」兩個字完全無關，沒有未平倉訊號時自然
    # 顯示「尚無記錄」，即使資料庫裡已經累積了一堆已平倉交易。
    # 這裡補一個明確只回傳「已平倉」交易、依平倉時間排序的函式，讓績效頁的
    # KPI（closed 筆數）跟底下列表用同一個母體，交易歷史頁也改抓這裡（見
    # app.py /api/performance 的呼叫端與 dashboard.html renderHistory()）。
    def get_closed_trades(self, limit: int = 100) -> List[Dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"get_closed_trades: {e}"); return []

    # ★ 新增：2026-09-16——見上方 _init_db() 的 actual_entry_price 欄位說明。
    def record_actual_fill(self, sig_id: str, actual_price: float) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("UPDATE signals SET actual_entry_price=? WHERE id=?", (actual_price, sig_id))
            return True
        except Exception as e:
            logger.error(f"record_actual_fill: {e}"); return False

    def get_slippage_stats(self) -> Dict:
        """統計目前累積到的「訊號參考價 vs 使用者實際成交價」滑價資料。
        買進時，實際成交價高於參考價＝多付（正滑價）；賣出(放空)時，實際成交價
        低於參考價＝滑價。回傳的 avg_slippage_pct 是正值代表平均而言使用者的
        實際成交比訊號參考價差，負值代表平均而言比參考價好。樣本數不足前
        （建議至少30筆以上）不應該拿這個數字去改動任何風控參數，只作為觀察用。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT direction, entry_price, actual_entry_price FROM signals "
                    "WHERE actual_entry_price IS NOT NULL AND entry_price IS NOT NULL AND entry_price != 0"
                ).fetchall()
            rows = [dict(r) for r in rows]
            diffs = []
            for r in rows:
                sign = 1 if r["direction"] == "buy" else -1
                diffs.append(sign * (r["actual_entry_price"] - r["entry_price"]) / r["entry_price"] * 100)
            if not diffs:
                return {"count": 0, "avg_slippage_pct": None, "note": "尚無使用者回報的實際成交價資料"}
            return {
                "count": len(diffs),
                "avg_slippage_pct": round(sum(diffs) / len(diffs), 3),
                "max_slippage_pct": round(max(diffs), 3),
                "min_slippage_pct": round(min(diffs), 3),
                "note": "樣本數<30前僅供觀察，不建議用來調整風控參數" if len(diffs) < 30 else "",
            }
        except Exception as e:
            logger.error(f"get_slippage_stats: {e}")
            return {"count": 0, "avg_slippage_pct": None, "note": "查詢失敗"}

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
    def _equity_stats(self, trades):
        """由已平倉交易（排除 ETF 放空）依平倉時間累加損益，回傳 (資金曲線, 最大回撤%, Sharpe)。
        起始資金用 ACCOUNT_BALANCE_TWD；樣本不足 10 筆時 Sharpe 不算（回 None），避免誤導。"""
        try:
            from config import ACCOUNT_BALANCE_TWD as _cap
        except Exception:
            _cap = 500000.0
        rows = [t for t in trades if not t.get("research_only") and t.get("pnl_twd") is not None and t.get("closed_at")]
        rows.sort(key=lambda t: str(t.get("closed_at")))
        bal = float(_cap); curve = [{"date": "起始", "balance": round(bal, 0)}]
        peak = bal; max_dd = 0.0; rets = []
        for t in rows:
            pnl = float(t.get("pnl_twd") or 0)
            rets.append(pnl / bal if bal else 0)
            bal += pnl
            curve.append({"date": str(t.get("closed_at"))[:10], "balance": round(bal, 0)})
            peak = max(peak, bal)
            if peak > 0:
                max_dd = max(max_dd, (peak - bal) / peak * 100)
        sharpe = None
        if len(rets) >= 10:
            import statistics as _stt
            sd = _stt.pstdev(rets)
            sharpe = round(_stt.mean(rets) / sd, 2) if sd else None
        return curve, round(max_dd, 2), sharpe

    def get_performance_summary(self) -> Dict:
        # ★ 修正：2026-09-16——改成 Postgres 後才發現的新 bug：psycopg2 送 SQL 時會把
        # 整段字串跑一次 Python 的 % 格式化來代入 %s 參數，SQL 內容裡原本寫死的
        # LIKE 'tp%' 這個萬用字元 % 會被誤判成格式化佔位符，因為外面沒有對應的參數
        # 可以代入，就丟出「tuple index out of range」——不是資料庫連線的問題，是
        # 這段 SQL 字串裡的字面 % 跟 psycopg2 的參數代換機制衝突。SQLite 用 ? 佔位符
        # 不會有這個問題，所以之前用 SQLite 從沒踩到。改成把 'tp%' 當成 bound
        # parameter 傳進去（兩種後端都支援, 也更安全），而不是寫死在 SQL 字串裡。
        try:
            # ★ 修正：2026-09-29——使用者回報「實盤紀錄裡有兩筆ETF空單，其中一
            # 檔是債券ETF，空單應該只算研究用途，不該混進實盤勝率」。台股 ETF
            # 代碼慣例一律以「00」開頭（跟 stock_universe._is_etf_code() 用的
            # 同一條規則），放空 ETF——尤其是債券 ETF——在多數券商根本不是一般
            # 散戶帳戶能直接下單的標的（要嘛不能融券、要嘛流動性/借券成本讓
            # 「照訊號價位計算的勝率」跟實際能不能成交脫節），把這類訊號的損益
            # 算進「實盤勝率」會讓這個數字看起來像是真的可執行績效，但其實
            # 混了一部分使用者根本無法照做的訊號。這裡把「ETF + 放空」的已平倉
            # 交易從勝率/損益的加總統計裡剔除（SQL 用 code LIKE '00%' 判斷
            # ETF，跟其餘程式碼同一套慣例），但不刪除資料本身——
            # get_closed_trades() 回傳的明細列表仍然看得到這些交易，只是額外
            # 標成 research_only，讓使用者自己判斷要不要參考，不是憑空消失。
            #
            # ★ 修正：2026-09-29——接上 Postgres 後這段又踩到跟上面同一種
            # % 衝突：etf_short_exclude 字串裡寫死的 code LIKE '00%'，那個
            # % 會被 psycopg2 的參數代換誤判，跟上面 'tp%' 的教訓一模一樣，
            # 只是這次是我自己剛加的程式碼踩進去。同樣改成 bound parameter，
            # 不要把任何字面 % 寫在 SQL 字串裡。
            etf_short_filter = "direction='sell' AND code LIKE ?"  # true＝要排除的 ETF 放空
            with self._conn() as conn:
                row = conn.execute(f"""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN result IN ('tp1','tp2','tp3') THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN result='sl' THEN 1 ELSE 0 END) as losses,
                           SUM(pnl_twd) as total_pnl,
                           AVG(CASE WHEN result LIKE ? THEN pnl_twd ELSE NULL END) as avg_win,
                           AVG(CASE WHEN result='sl' THEN pnl_twd ELSE NULL END) as avg_loss
                    FROM signals WHERE status='closed' AND NOT ({etf_short_filter})
                """, ("tp%", "00%")).fetchone()
                excluded_row = conn.execute(f"""
                    SELECT COUNT(*) as n FROM signals WHERE status='closed' AND {etf_short_filter}
                """, ("00%",)).fetchone()
            total=row["total"] or 0; wins=row["wins"] or 0; losses=row["losses"] or 0
            closed=wins+losses
            excluded_etf_shorts = excluded_row["n"] or 0
            recent = self.get_closed_trades(100)
            for t in recent:
                t["research_only"] = (t.get("direction")=="sell" and str(t.get("code","")).startswith("00"))
            curve, max_dd, sharpe = self._equity_stats(recent)
            return {
                "equity_curve": curve, "max_drawdown": max_dd, "sharpe": sharpe,
                "total":      total, "closed": closed, "pending": total-closed,
                "wins":       wins,  "losses": losses,
                "win_rate":   round(wins/max(closed,1)*100,1),
                "total_pnl":  round(row["total_pnl"] or 0,0),
                "avg_win":    round(row["avg_win"]  or 0,0),
                "avg_loss":   round(row["avg_loss"] or 0,0),
                # ★ 修正：2026-09-29——原本是 get_recent_signals(20)（近30天所有
                # 訊號，不分 status），跟上面 total/wins/losses 這幾個只算
                # status='closed' 的統計對不上母體，見 get_closed_trades() 的
                # 說明。改成同樣只抓已平倉交易，兩邊數字才會一致。列表本身仍
                # 包含 ETF 空單（標 research_only=true），只有上面的加總統計
                # 排除它們。
                "recent_trades": recent,
                "excluded_etf_short_count": excluded_etf_shorts,
                # ★ 新增：2026-09-16——三方AI交叉比對後續修正，見 get_slippage_stats()
                # 與 get_winrate_by_weekly_bias() 的定義說明。掛在 /api/performance
                # 既有回應裡，不需要另外開新端點，網站/未來分析都能直接拿到。
                "slippage_stats": self.get_slippage_stats(),
                "winrate_by_weekly_bias": self.get_winrate_by_weekly_bias(),
            }
        except Exception as e:
            logger.error(f"get_performance_summary: {e}")
            return {"total":0,"wins":0,"losses":0,"win_rate":0,"total_pnl":0,"recent_trades":[],
                     "slippage_stats":{"count":0,"avg_slippage_pct":None},"winrate_by_weekly_bias":{}}

    # ★ 新增：2026-09-16——回應稽核報告 D 部分（回測沒有真正驗證多時框/週線
    # 邏輯）的短期低成本方案：weekly_bias 欄位其實從一開始就已經跟著每筆訊號
    # 存進資料庫（見 save_signal 的 cols 清單），只是從來沒有人真的把它拿出來
    # 跟後續的實際損益做交叉統計——這裡補上這個查詢，讓「週線多頭的訊號勝率
    # 是不是真的比週線中性/空頭高」這個問題，可以直接用實盤累積的資料回答，
    # 不需要動回測引擎、也不需要重建歷史多時框資料集。累積筆數不足時（建議
    # 至少每組20筆以上）不應該拿來做任何參數調整的依據，純觀察用。
    def get_winrate_by_weekly_bias(self) -> Dict:
        try:
            with self._conn() as conn:
                rows = conn.execute("""
                    SELECT weekly_bias,
                           COUNT(*) as closed,
                           SUM(CASE WHEN result IN ('tp1','tp2','tp3') THEN 1 ELSE 0 END) as wins
                    FROM signals WHERE status='closed' AND weekly_bias IS NOT NULL AND weekly_bias != ''
                    GROUP BY weekly_bias
                """).fetchall()
            out = {}
            for r in rows:
                r = dict(r)
                closed = r["closed"] or 0
                wins = r["wins"] or 0
                out[r["weekly_bias"]] = {
                    "closed": closed, "wins": wins,
                    "win_rate": round(wins / max(closed, 1) * 100, 1),
                    "note": "樣本數<20前僅供觀察" if closed < 20 else "",
                }
            return out
        except Exception as e:
            logger.error(f"get_winrate_by_weekly_bias: {e}")
            return {}

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

    # ── K線持久化快取 ──
    # ★ 新增：2026-09-24——回應使用者要求：fetch_ohlcv() 先前每次呼叫都對
    # yfinance 重新下載整段歷史（daily 一年、weekly 五年、hourly 90天），
    # 這是先前稽核就記錄過的「單次全市場掃描要7分鐘」最大瓶頸——只有進程內的
    # 記憶體 TTL 快取（見 data_fetcher._cache），每次 Render 重新部署/重啟
    # process 就整個歸零，隔天又要重新全部下載一次。改成把K棒存進這裡（跟
    # signals 用同一個 Postgres，真正持久化、不隨部署清空），之後只需要對
    # yfinance 要「上次快取日期之後」的新增K棒（通常1~2根），大幅縮短每檔
    # 股票的等待時間，也降低對外部 API 的請求量/被限流風險。
    def get_ohlcv_last_date(self, ticker: str, tf_key: str) -> Optional[str]:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT MAX(bar_date) as d FROM ohlcv_bars WHERE ticker=? AND tf_key=?",
                    (ticker, tf_key)
                ).fetchone()
            return row["d"] if row and row["d"] else None
        except Exception as e:
            logger.warning(f"get_ohlcv_last_date {ticker}/{tf_key}: {e}")
            return None

    # ── 官方成交量（張）：Yahoo 的成交量與證交所／櫃買官方數字常差 5～15%，K 線改以官方為準 ──
    _ov_ready = False

    def _ensure_ov(self):
        if self._ov_ready:
            return
        try:
            with self._conn() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS official_volume (code TEXT, bar_date TEXT, lots BIGINT, updated_at TEXT, PRIMARY KEY (code, bar_date))")
            self._ov_ready = True
        except Exception as e:
            logger.warning(f"_ensure_ov: {e}")

    def upsert_official_volumes(self, rows):
        """rows: [(code, 'YYYY-MM-DD', lots)]"""
        if not rows:
            return
        self._ensure_ov()
        try:
            now = datetime.now(timezone.utc).isoformat()
            if USE_PG:
                sql = ("INSERT INTO official_volume(code,bar_date,lots,updated_at) VALUES(?,?,?,?) "
                       "ON CONFLICT (code,bar_date) DO UPDATE SET lots=EXCLUDED.lots,updated_at=EXCLUDED.updated_at")
            else:
                sql = "INSERT OR REPLACE INTO official_volume(code,bar_date,lots,updated_at) VALUES(?,?,?,?)"
            with self._conn() as conn:
                for c, d, l in rows:
                    conn.execute(sql, (c, d, int(l), now))
        except Exception as e:
            logger.warning(f"upsert_official_volumes: {e}")

    _notice_ready = False

    def claim_notice(self, key: str, ttl_sec: int = 600) -> bool:
        """跨實例去重：ttl_sec 秒內同一個 key 只有第一個呼叫者回傳 True（可以發送）。
        部署時 Render 會讓新舊實例短暫並存，排程各觸發一次，靠共用資料庫擋掉重複推播。
        資料庫出錯時採「放行」（寧可偶爾重複，也不能漏發）。"""
        import time as _t
        try:
            if not self._notice_ready:
                with self._conn() as conn:
                    conn.execute("CREATE TABLE IF NOT EXISTS notice_log (k TEXT PRIMARY KEY, ts BIGINT)")
                self._notice_ready = True
            now = int(_t.time())
            with self._conn() as conn:
                conn.execute("DELETE FROM notice_log WHERE k=? AND ts<?", (key, now - ttl_sec))
                if USE_PG:
                    cur = conn.execute("INSERT INTO notice_log(k,ts) VALUES(?,?) ON CONFLICT (k) DO NOTHING", (key, now))
                else:
                    cur = conn.execute("INSERT OR IGNORE INTO notice_log(k,ts) VALUES(?,?)", (key, now))
                return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.warning(f"claim_notice: {e}")
            return True

    def get_official_volumes(self, code: str, since: str = "2000-01-01") -> Dict[str, int]:
        self._ensure_ov()
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT bar_date, lots FROM official_volume WHERE code=? AND bar_date>=?", (code, since)).fetchall()
            return {r["bar_date"]: int(r["lots"]) for r in rows}
        except Exception as e:
            logger.warning(f"get_official_volumes: {e}")
            return {}

    def get_daily_closes_recent(self, days: int = 10) -> Dict[str, List]:
        """一次取出全部股票最近幾天的日線收盤（市場廣度用，同一天口徑）。回傳 {ticker: [(bar_date, close), ...] 由舊到新}"""
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT MAX(bar_date) as d FROM ohlcv_bars WHERE tf_key='daily'").fetchone()
                mx = row["d"] if row and row["d"] else None
                if not mx:
                    return {}
                from datetime import datetime as _d, timedelta as _t
                since = (_d.strptime(mx[:10], "%Y-%m-%d") - _t(days=days)).strftime("%Y-%m-%d")
                rows = conn.execute("SELECT ticker, bar_date, close FROM ohlcv_bars WHERE tf_key='daily' AND bar_date>=? ORDER BY ticker, bar_date",
                                    (since,)).fetchall()
            out: Dict[str, List] = {}
            for r in rows:
                out.setdefault(r["ticker"], []).append((r["bar_date"], r["close"]))
            return out
        except Exception as e:
            logger.warning(f"get_daily_closes_recent: {e}")
            return {}

    def upsert_ohlcv_bars(self, ticker: str, tf_key: str, bars: List[Dict]):
        """bars: [{date,open,high,low,close,volume}, ...]。用 UPSERT，同一天
        重複寫入（例如盤中抓到的當日殘缺K棒，收盤後再抓一次拿到定案數字）
        會直接覆蓋掉舊值，不會產生重複列。"""
        if not bars:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            if USE_PG:
                sql = ("INSERT INTO ohlcv_bars(ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT (ticker,tf_key,bar_date) DO UPDATE SET "
                       "open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,"
                       "volume=EXCLUDED.volume,updated_at=EXCLUDED.updated_at")
            else:
                sql = ("INSERT OR REPLACE INTO ohlcv_bars"
                       "(ticker,tf_key,bar_date,open,high,low,close,volume,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)")
            with self._conn() as conn:
                for b in bars:
                    conn.execute(sql, (ticker, tf_key, b["date"], b["open"], b["high"],
                                        b["low"], b["close"], b["volume"], now))
        except Exception as e:
            logger.warning(f"upsert_ohlcv_bars {ticker}/{tf_key}: {e}")

    def get_cached_ohlcv_bars(self, ticker: str, tf_key: str, limit: int = 300) -> List[Dict]:
        """回傳依日期由舊到新排序的K棒清單，最多取最新 limit 根。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT bar_date,open,high,low,close,volume FROM ohlcv_bars "
                    "WHERE ticker=? AND tf_key=? ORDER BY bar_date DESC LIMIT ?",
                    (ticker, tf_key, limit)
                ).fetchall()
            rows = [dict(r) for r in rows]
            rows.reverse()
            return rows
        except Exception as e:
            logger.warning(f"get_cached_ohlcv_bars {ticker}/{tf_key}: {e}")
            return []

    def get_ohlcv_cache_stats(self) -> Dict:
        """給 /api/diagnostics 用：目前快取涵蓋幾檔股票、最舊/最新更新時間，
        讓「快取到底有沒有在運作」這件事可以直接從網站看到，不用查資料庫。"""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT ticker) as tickers, COUNT(*) as bars, "
                    "MAX(updated_at) as last_updated FROM ohlcv_bars"
                ).fetchone()
            return dict(row) if row else {"tickers": 0, "bars": 0, "last_updated": None}
        except Exception as e:
            logger.warning(f"get_ohlcv_cache_stats: {e}")
            return {"tickers": 0, "bars": 0, "last_updated": None}

    # ── 月營收歷史（回測用，真實歷史資料）──
    # ★ 新增：2026-09-24——使用者明確要求「用真實資料回測，不要假數據」，
    # 且要求把能補的都補上。原本 fundamentals.fetch_monthly_revenue_map() 只能
    # 抓「當下這個月」的全市場快照，系統裡完全沒有月營收的歷史時間序列，導致
    # 個股基本面硬性過濾這項功能完全沒辦法回測。這裡新增一張表持久化保存從
    # MOPS歷史月營收頁面（t21sc03，見 fundamentals.py 說明）回填抓到的資料，
    # 之後回測時可以用「當下那天，已經公告過的最新一期月營收」做真正的
    # 事後（point-in-time）查詢，不是用未來才會公告的資料回頭作弊。
    def upsert_monthly_revenue_history(self, rows: List[Dict]):
        """rows: [{ticker,period,yoy_pct,mom_pct,market}, ...]，period 格式 'YYYY-MM'。"""
        if not rows:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            if USE_PG:
                sql = ("INSERT INTO monthly_revenue_history(ticker,period,yoy_pct,mom_pct,market,updated_at) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT (ticker,period) DO UPDATE SET "
                       "yoy_pct=EXCLUDED.yoy_pct,mom_pct=EXCLUDED.mom_pct,market=EXCLUDED.market,"
                       "updated_at=EXCLUDED.updated_at")
            else:
                sql = ("INSERT OR REPLACE INTO monthly_revenue_history"
                       "(ticker,period,yoy_pct,mom_pct,market,updated_at) VALUES(?,?,?,?,?,?)")
            with self._conn() as conn:
                for r in rows:
                    conn.execute(sql, (r["ticker"], r["period"], r.get("yoy_pct"),
                                        r.get("mom_pct"), r.get("market",""), now))
        except Exception as e:
            logger.warning(f"upsert_monthly_revenue_history: {e}")

    def get_monthly_revenue_periods_covered(self) -> List[str]:
        """回傳已經回填過的期別清單（'YYYY-MM'），讓回填程式可以跳過已經抓過的
        月份，重跑時不用重複打 MOPS，也不會因為中途失敗就要整個重來。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT period FROM monthly_revenue_history ORDER BY period"
                ).fetchall()
            return [r["period"] for r in rows]
        except Exception as e:
            logger.warning(f"get_monthly_revenue_periods_covered: {e}")
            return []

    def get_revenue_asof(self, ticker: str, max_period: str) -> Optional[Dict]:
        """回傳某檔股票「在 max_period（含）之前，最新一期」的月營收資料——
        對應回測裡「這一天系統看得到的最新已公告營收是哪一期」，見
        fundamentals.check_fundamental_hard_filter_asof() 的申報時間差說明。"""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT period,yoy_pct,mom_pct FROM monthly_revenue_history "
                    "WHERE ticker=? AND period<=? ORDER BY period DESC LIMIT 1",
                    (ticker, max_period)
                ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.warning(f"get_revenue_asof {ticker}: {e}")
            return None

    # ── 融資餘額每日歷史（回測用，真實歷史資料）──
    # ★ 新增：2026-09-24——同上，原本 data_fetcher.fetch_margin_change() 只從
    # 今天開始持久化「昨天餘額」，完全沒有歷史數列可以回測。這裡改成直接向
    # TWSE MI_MARGN 用歷史日期查詢（已用 WebFetch 實測 date= 參數真的能查到
    # 過去某天的資料，且單一天的回應本身就同時附「前日餘額」跟「今日餘額」，
    # 不需要逐日往前串接），回填出一份「每個交易日的融資餘額日變動%」，
    # 存進這張表供回測直接查表使用。
    def upsert_margin_chg_daily(self, bar_date: str, balance: int, chg_pct: float):
        try:
            now = datetime.now(timezone.utc).isoformat()
            if USE_PG:
                sql = ("INSERT INTO margin_chg_daily_history(bar_date,balance,chg_pct,updated_at) "
                       "VALUES(?,?,?,?) ON CONFLICT (bar_date) DO UPDATE SET "
                       "balance=EXCLUDED.balance,chg_pct=EXCLUDED.chg_pct,updated_at=EXCLUDED.updated_at")
            else:
                sql = ("INSERT OR REPLACE INTO margin_chg_daily_history"
                       "(bar_date,balance,chg_pct,updated_at) VALUES(?,?,?,?)")
            with self._conn() as conn:
                conn.execute(sql, (bar_date, balance, chg_pct, now))
        except Exception as e:
            logger.warning(f"upsert_margin_chg_daily {bar_date}: {e}")

    def get_margin_chg_dates_covered(self) -> List[str]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT bar_date FROM margin_chg_daily_history ORDER BY bar_date"
                ).fetchall()
            return [r["bar_date"] for r in rows]
        except Exception as e:
            logger.warning(f"get_margin_chg_dates_covered: {e}")
            return []

    def get_margin_chg_map(self) -> Dict[str, float]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT bar_date,chg_pct FROM margin_chg_daily_history"
                ).fetchall()
            return {r["bar_date"]: r["chg_pct"] for r in rows}
        except Exception as e:
            logger.warning(f"get_margin_chg_map: {e}")
            return {}

    # ── 三大法人每日買賣超（多日累積，用來算連續買超／賣超）──
    # ★ 新增：2026-10-06——T86 一次只給單日，連買天數、累計買超都需要歷史，
    # 所以每天收盤後把全市場（上市）外資／投信／三大法人淨買賣超（單位：張）存起來。
    def upsert_inst_daily(self, trade_date: str, rows: Dict) -> int:
        n = 0
        try:
            if USE_PG:
                sql = ("INSERT INTO inst_daily(trade_date,code,name,foreign_net,trust_net,total_net) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT (trade_date,code) DO UPDATE SET "
                       "name=EXCLUDED.name,foreign_net=EXCLUDED.foreign_net,"
                       "trust_net=EXCLUDED.trust_net,total_net=EXCLUDED.total_net")
            else:
                sql = ("INSERT OR REPLACE INTO inst_daily"
                       "(trade_date,code,name,foreign_net,trust_net,total_net) VALUES(?,?,?,?,?,?)")
            with self._conn() as conn:
                for code, v in rows.items():
                    conn.execute(sql, (trade_date, code, v.get("name"), int(v.get("foreign_net", 0)),
                                       int(v.get("trust_net", 0)), int(v.get("total_net", 0))))
                    n += 1
        except Exception as e:
            logger.warning(f"upsert_inst_daily {trade_date}: {e}")
        return n

    def get_inst_dates(self, limit: int = 60) -> List[str]:
        """已累積的交易日（新到舊）。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT trade_date FROM inst_daily ORDER BY trade_date DESC LIMIT ?", (limit,)
                ).fetchall()
            return [r["trade_date"] for r in rows]
        except Exception as e:
            logger.warning(f"get_inst_dates: {e}")
            return []

    def get_inst_since(self, min_date: str) -> List[Dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT trade_date,code,name,foreign_net,trust_net,total_net FROM inst_daily "
                    "WHERE trade_date>=? ORDER BY trade_date", (min_date,)
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"get_inst_since: {e}")
            return []

    # ── 獲利品質（季報毛利率/營業利益率快照，用來算「趨勢」）──
    # ★ 新增：2026-09-28——使用者要求接「獲利品質」評分。TWSE OpenAPI 的季報
    # 端點（opendata/t187ap06_L_ci）每次只回傳「最新一期」數字，沒有歷史序列，
    # 沒辦法從單一次 API 回應算出「毛利率有沒有變差」這種趨勢資訊——所以這裡
    # 每次抓到新一期資料就存一筆快照，等下一季資料進來時，就能跟這裡存的
    # 上一筆比較算出變化量。period 格式固定是「年度Q季別」（例如"115Q2"），
    # 同樣位數下字串排序等同時間排序，可以直接用字串比較/ORDER BY。
    def save_quarterly_margin(self, code: str, period: str,
                               gross_margin_pct: Optional[float], operating_margin_pct: Optional[float],
                               cum_revenue: Optional[float] = None, cum_gross_profit: Optional[float] = None,
                               cum_operating_income: Optional[float] = None) -> bool:
        """cum_revenue/cum_gross_profit/cum_operating_income 是「累計原始數字」
        （不是比率），用來讓下一季呼叫時反推單季數字（見上方 _init_db() 遷移
        的說明）。舊呼叫端不傳這三個參數時是 None，等於只存比率，沒辦法反推
        單季——這是為了不強制所有呼叫端都要改，但獲利品質這條路徑（唯一
        呼叫端）一定會傳。"""
        try:
            now = datetime.now(timezone.utc).isoformat()
            if USE_PG:
                sql = ("INSERT INTO quarterly_margin_snapshot"
                       "(code,period,gross_margin_pct,operating_margin_pct,"
                       "cum_revenue,cum_gross_profit,cum_operating_income,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT (code,period) DO UPDATE SET "
                       "gross_margin_pct=EXCLUDED.gross_margin_pct,"
                       "operating_margin_pct=EXCLUDED.operating_margin_pct,"
                       "cum_revenue=EXCLUDED.cum_revenue,"
                       "cum_gross_profit=EXCLUDED.cum_gross_profit,"
                       "cum_operating_income=EXCLUDED.cum_operating_income,"
                       "updated_at=EXCLUDED.updated_at")
            else:
                sql = ("INSERT OR REPLACE INTO quarterly_margin_snapshot"
                       "(code,period,gross_margin_pct,operating_margin_pct,"
                       "cum_revenue,cum_gross_profit,cum_operating_income,updated_at) VALUES(?,?,?,?,?,?,?,?)")
            with self._conn() as conn:
                conn.execute(sql, (code, period, gross_margin_pct, operating_margin_pct,
                                    cum_revenue, cum_gross_profit, cum_operating_income, now))
            return True
        except Exception as e:
            logger.warning(f"save_quarterly_margin {code}/{period}: {e}")
            return False

    def get_prev_quarterly_margin(self, code: str, period: str) -> Optional[Dict]:
        """回傳指定股票在 period 之前、最近一筆有紀錄的季度快照，沒有歷史紀錄
        （系統第一次看到這檔股票的季報資料，這是正常情況，不代表有問題）
        回傳 None，呼叫端要把「沒有上一季可比較」跟「有比較、變化量是0」
        分開處理。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM quarterly_margin_snapshot WHERE code=? AND period<? "
                    "ORDER BY period DESC LIMIT 1",
                    (code, period)
                ).fetchall()
            return dict(rows[0]) if rows else None
        except Exception as e:
            logger.warning(f"get_prev_quarterly_margin {code}/{period}: {e}")
            return None

    def get_quarterly_margin_snapshot(self, code: str, period: str) -> Optional[Dict]:
        """精確查某一檔股票、某一個 period（例如"115Q1"）的快照，跟
        get_prev_quarterly_margin()「period之前最近一筆」不同，這裡要求
        完全對上——反推單季數字時（見 fundamentals.py 說明）需要精確指定
        「上一季」跟「上上一季」各自的累計數字，不能用「最近一筆」代替
        （中間可能跳過某一季沒抓到資料，用「最近一筆」會反推出錯誤的
        單季數字）。找不到精確對應的那一季就回傳 None，呼叫端要能處理
        「中間有缺一季資料」這個正常情況。"""
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM quarterly_margin_snapshot WHERE code=? AND period=? LIMIT 1",
                    (code, period)
                ).fetchall()
            return dict(rows[0]) if rows else None
        except Exception as e:
            logger.warning(f"get_quarterly_margin_snapshot {code}/{period}: {e}")
            return None

    # ── Telegram 訂閱名單 ──
    # ★ 新增：2026-09-29（跨AI覆核發現，經 grep/Read 驗證屬實）——telegram_bot.py
    # 原本把 free/paid 訂閱名單存在本機檔案 instance/subscribers.json，跟這個
    # 檔案原本要解決的 SQLite 問題是同一類：Render 這個 Web Service 沒有
    # Persistent Disk，每次重新部署容器都會重建，本機檔案會被清空，導致
    # 使用者 /start 訂閱之後，下次部署就被靜默取消訂閱，使用者跟機主都不會
    # 收到任何通知。改存進這個已經是真正持久化的資料庫（Postgres／SQLite雙
    # 後端），跟其他狀態走同一套持久化機制。
    def add_subscriber(self, chat_id: str, tier: str = "free") -> bool:
        try:
            chat_id = str(chat_id)
            now = datetime.now(timezone.utc).isoformat()
            with self._conn() as conn:
                if USE_PG:
                    conn.execute(
                        "INSERT INTO subscribers (chat_id, tier, added_at) VALUES (?, ?, ?) "
                        "ON CONFLICT (chat_id) DO UPDATE SET tier=EXCLUDED.tier",
                        (chat_id, tier, now)
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO subscribers (chat_id, tier, added_at) VALUES (?, ?, ?)",
                        (chat_id, tier, now)
                    )
            return True
        except Exception as e:
            logger.warning(f"add_subscriber {chat_id}: {e}")
            return False

    def remove_subscriber(self, chat_id: str) -> bool:
        try:
            chat_id = str(chat_id)
            with self._conn() as conn:
                conn.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
            return True
        except Exception as e:
            logger.warning(f"remove_subscriber {chat_id}: {e}")
            return False

    def get_subscribers(self) -> Dict[str, List[str]]:
        """回傳 {"free":[...], "paid":[...]}——admin 名單不在這裡存，
        由呼叫端（telegram_bot._load_subscribers）用 TELEGRAM_CHAT_ID
        動態併入，跟原本行為一致。"""
        result = {"free": [], "paid": []}
        try:
            with self._conn() as conn:
                rows = conn.execute("SELECT chat_id, tier FROM subscribers").fetchall()
            for row in rows:
                row = dict(row)
                tier = row.get("tier") or "free"
                result.setdefault(tier, []).append(str(row["chat_id"]))
        except Exception as e:
            logger.warning(f"get_subscribers: {e}")
        return result

    # ── 網頁通知中心（與 Telegram 推播同步記錄）──
    def add_event(self, kind: str, title: str, body: str = "", ticker: str = "") -> bool:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO events (ts, kind, title, body, ticker) VALUES (?, ?, ?, ?, ?)",
                    (now, kind, title, body, ticker or ""),
                )
            return True
        except Exception as e:
            logger.warning(f"add_event {kind}: {e}")
            return False

    def get_events(self, limit: int = 60) -> List[Dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, ts, kind, title, body, ticker FROM events ORDER BY ts DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"get_events: {e}")
            return []

    # ── 觀察清單（後端持久化，跨裝置）──
    def watchlist_get(self) -> List[Dict]:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT ticker, code, name, note, added_at FROM watchlist ORDER BY added_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"watchlist_get: {e}")
            return []

    def watchlist_add(self, ticker: str, code: str = "", name: str = "", note: str = "") -> bool:
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._conn() as conn:
                if USE_PG:
                    conn.execute(
                        "INSERT INTO watchlist (ticker, code, name, note, added_at) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (ticker) DO UPDATE SET name=EXCLUDED.name, code=EXCLUDED.code",
                        (ticker, code, name, note, now),
                    )
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO watchlist (ticker, code, name, note, added_at) VALUES (?, ?, ?, ?, ?)",
                        (ticker, code, name, note, now),
                    )
            return True
        except Exception as e:
            logger.warning(f"watchlist_add {ticker}: {e}")
            return False

    def watchlist_remove(self, ticker: str) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM watchlist WHERE ticker=?", (ticker,))
            return True
        except Exception as e:
            logger.warning(f"watchlist_remove {ticker}: {e}")
            return False

    def is_paid_subscriber(self, chat_id: str) -> bool:
        try:
            chat_id = str(chat_id)
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT 1 FROM subscribers WHERE chat_id=? AND tier='paid'", (chat_id,)
                ).fetchall()
            return bool(rows)
        except Exception as e:
            logger.warning(f"is_paid_subscriber {chat_id}: {e}")
            return False


store = StateStore()


def _rev_series(self, code: str, n: int = 8):
    """某檔最近 n 期的月營收年增／月增（新到舊）。"""
    try:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT period,yoy_pct,mom_pct FROM monthly_revenue_history WHERE ticker=? ORDER BY period DESC LIMIT ?",
                (code, n)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_revenue_series {code}: {e}")
        return []

try:
    StateStore.get_revenue_series = _rev_series
except NameError:
    pass


def _scan_hist(self, n: int = 5):
    try:
        with self._conn() as conn:
            rows = conn.execute("SELECT scan_date,scanned,signals_found,signals_sent,duration_min,errors FROM scan_history ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"get_scan_history: {e}")
        return []

StateStore.get_scan_history = _scan_hist
