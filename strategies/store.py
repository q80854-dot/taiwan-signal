"""策略假想訊號的獨立儲存（階段 2b-1）。

獨立表 strategy_signals，鍵 (bar_date, ticker, strategy_name)——避開 shadow_signals 的
UNIQUE(bar_date,ticker)（同一檔同一天只能存一列、會與 Champion 候選衝突），且不動 shadow 語意、
不重構主鍵（SH01 已擱置）。純 shadow 研究：不發訊號、不進風控、不影響線上。

結算沿用既有單一權威模擬器 execution.simulate_staged_exit（分批出場＋到期窗）與 shadow 的成本
模型，算出 champion_r（與 Champion 同口徑的分批淨 R），可直接與正式策略比較。
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
_TPE = timezone(timedelta(hours=8))
_ready = False


def _ddl():
    from state_store import USE_PG
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return f"""
    CREATE TABLE IF NOT EXISTS strategy_signals (
        id {pk}, bar_date TEXT, ticker TEXT, code TEXT, name TEXT, strategy_name TEXT,
        direction TEXT, entry REAL, stop REAL, tp1 REAL, tp2 REAL, tp3 REAL, score REAL, reason TEXT,
        status TEXT DEFAULT 'pending', result TEXT, exit_price REAL, exit_date TEXT,
        champion_r REAL, created_at TEXT,
        UNIQUE (bar_date, ticker, strategy_name)
    );
    CREATE INDEX IF NOT EXISTS idx_strat_status ON strategy_signals(status);
    CREATE INDEX IF NOT EXISTS idx_strat_name ON strategy_signals(strategy_name, status);
    """


def ensure_table():
    global _ready
    if _ready:
        return
    from state_store import store
    with store._conn() as conn:
        conn.executescript(_ddl())
    _ready = True


def record_candidates(bar_date, ticker, stock_info, strategy_plans) -> int:
    """寫入某檔某日各策略的假想訊號。strategy_plans：list[(strategy_name, plan dict)]。
    重複的 (bar_date,ticker,strategy_name) 會被略過。回傳實際新增筆數。"""
    if not strategy_plans:
        return 0
    ensure_table()
    from state_store import store
    code = (stock_info or {}).get("code") or ticker.replace(".TW", "").replace(".TWO", "")
    name = (stock_info or {}).get("name") or ""
    now = datetime.now(_TPE).isoformat()
    with store._conn() as conn:
        before = (conn.execute("SELECT COUNT(*) AS n FROM strategy_signals").fetchone() or {"n": 0})["n"]
        for sname, plan in strategy_plans:
            d = "buy" if plan.get("direction") in ("long", "buy") else "sell"
            try:
                conn.execute(
                    "INSERT INTO strategy_signals (bar_date,ticker,code,name,strategy_name,direction,"
                    "entry,stop,tp1,tp2,tp3,score,reason,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (bar_date,ticker,strategy_name) DO NOTHING",
                    (bar_date, ticker, code, name, sname, d, plan.get("entry"), plan.get("stop"),
                     plan.get("tp1"), plan.get("tp2"), plan.get("tp3"), plan.get("score"),
                     plan.get("reason"), "pending", now))
            except Exception as e:
                logger.debug(f"strategy record {sname}/{ticker}: {e}")
        after = (conn.execute("SELECT COUNT(*) AS n FROM strategy_signals").fetchone() or {"n": 0})["n"]
    return max(0, after - before)


def resolve_pending(max_rows=5000) -> dict:
    """結算 pending 的策略假想訊號：以分批出場（到期窗）＋shadow 成本算 champion_r。
    仍有未出場部位且尚未到期者留 pending（下次有新 K 棒再結算），與 scanner 正式結算同邏輯。"""
    ensure_table()
    import shadow
    import execution
    from state_store import store
    from config import CIRCUIT_BREAKER
    expire_days = CIRCUIT_BREAKER.get("signal_expire_days", 15)
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM strategy_signals WHERE status='pending' ORDER BY bar_date LIMIT ?",
            (max_rows,)).fetchall()]
    if not rows:
        return {"updated": 0, "closed": 0, "rows": 0}
    since = (datetime.strptime(min(r["bar_date"] for r in rows), "%Y-%m-%d")
             - timedelta(days=10)).strftime("%Y-%m-%d")
    bars_by = shadow._load_bars(sorted({r["ticker"] for r in rows}), since)
    pnl_fn = shadow._shadow_staged_pnl_factory()
    today = datetime.now(_TPE).replace(tzinfo=None)
    updates, closed = [], 0
    for r in rows:
        if not shadow._risk_ok(r.get("entry"), r.get("stop")):
            updates.append(("UPDATE strategy_signals SET status='unresolved', result='bad_risk' WHERE id=?",
                            (r["id"],)))
            continue
        bars = bars_by.get(r["ticker"])
        if not bars:
            continue
        after = [b for b in bars if b["bar_date"][:10] > r["bar_date"]]
        if not after:
            continue
        risk = abs(r["entry"] - r["stop"])
        fill = after[0].get("open") or r["entry"]
        sim = execution.simulate_staged_exit(
            after, entry=fill, sl=r["stop"], tp1=r.get("tp1"), tp2=r.get("tp2"), tp3=r.get("tp3"),
            direction=r["direction"], shares=3000, gen_date=r["bar_date"], pnl_fn=pnl_fn,
            expire_days=expire_days)
        if not sim:
            continue
        has_open = any(l["stage"] == "expired" for l in sim["legs"])
        try:
            aged_out = (today - datetime.strptime(r["bar_date"], "%Y-%m-%d")).days >= expire_days
        except Exception:
            aged_out = True
        if has_open and not aged_out:
            continue                       # 尚有未出場部位、未到期：留著下次結算
        champ = round(sim["pnl"] / (risk * 3000), 3) if risk else None
        updates.append((
            "UPDATE strategy_signals SET status='closed', result=?, exit_price=?, exit_date=?, champion_r=? WHERE id=?",
            (sim["result"], sim["exit_price"], sim["exit_date"], champ, r["id"])))
        closed += 1
    if updates:
        with store._conn() as conn:
            for q, v in updates:
                conn.execute(q, v)
    return {"updated": len(updates), "closed": closed, "rows": len(rows)}


def overview() -> dict:
    """各策略的 shadow 績效摘要（筆數、已結算、平均 champion_r）。純研究、依策略分列。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT strategy_name, COUNT(*) AS total, "
            "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed, "
            "AVG(CASE WHEN status='closed' THEN champion_r ELSE NULL END) AS avg_champion_r "
            "FROM strategy_signals GROUP BY strategy_name ORDER BY strategy_name").fetchall()]
    return {"strategies": [{"strategy_name": r["strategy_name"], "total": int(r["total"] or 0),
                            "closed": int(r["closed"] or 0),
                            "avg_champion_r": round(r["avg_champion_r"], 3) if r["avg_champion_r"] is not None else None}
                           for r in rows],
            "note": "純 shadow 研究、未接線上；champion_r＝與正式分批出場同口徑的淨 R，樣本累積中。"}
