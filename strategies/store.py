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
        champion_r REAL, mae_r REAL, mfe_r REAL, created_at TEXT,
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
    # 既有部署補欄（冪等）：逐筆 MAE/MFE（2026-11-12 起記錄；舊列 NULL＝誠實「無紀錄」，不回填）
    store._migrate_add_column("strategy_signals", "mae_r", "REAL")
    store._migrate_add_column("strategy_signals", "mfe_r", "REAL")
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
        # 逐筆 MAE/MFE（有號極值，R 計）：持有窗（首根之後到出場日）內最不利/最有利價格偏移。
        # 基準＝實際 fill（首根開盤）、分母＝|entry−stop|；價格路徑極值、不含費稅（與 champion_r
        # 的費後口徑不同，展示端須分開標示）。算不出來存 NULL，不猜值。
        mae = mfe = None
        try:
            _xd = sim.get("exit_date")
            win = [b for b in after if not _xd or b["bar_date"][:10] <= _xd]
            his = [b["high"] for b in win if b.get("high") is not None]
            los = [b["low"] for b in win if b.get("low") is not None]
            if his and los and risk:
                if r["direction"] == "buy":
                    mae = round((min(los) - fill) / risk, 3)
                    mfe = round((max(his) - fill) / risk, 3)
                else:
                    mae = round((fill - max(his)) / risk, 3)
                    mfe = round((fill - min(los)) / risk, 3)
        except Exception:
            mae = mfe = None
        updates.append((
            "UPDATE strategy_signals SET status='closed', result=?, exit_price=?, exit_date=?, champion_r=?, mae_r=?, mfe_r=? WHERE id=?",
            (sim["result"], sim["exit_price"], sim["exit_date"], champ, mae, mfe, r["id"])))
        closed += 1
    if updates:
        with store._conn() as conn:
            for q, v in updates:
                conn.execute(q, v)
    return {"updated": len(updates), "closed": closed, "rows": len(rows)}


MIN_EVAL_SAMPLES = 30          # 結論門檻：已結算 < 此數一律保留為「資料不足」，不下結論（§8.5）


def overview() -> dict:
    """各策略的 shadow 績效摘要（§8.5 影子展示用）。純研究、依策略分列，依實際可得資料提供：
    總筆數/未結/已結算/勝負、Champion 同口徑平均淨 R、開始與最近追蹤日。
    回撤/換手/風險事件逐筆資料尚未逐欄記錄 → 列為資料契約缺口，不前端猜值。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT strategy_name, COUNT(*) AS total, "
            "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed, "
            "SUM(CASE WHEN status='closed' AND champion_r > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN status='closed' AND champion_r <= 0 THEN 1 ELSE 0 END) AS losses, "
            "AVG(CASE WHEN status='closed' THEN champion_r ELSE NULL END) AS avg_champion_r, "
            "AVG(CASE WHEN status='closed' THEN mae_r ELSE NULL END) AS avg_mae_r, "
            "AVG(CASE WHEN status='closed' THEN mfe_r ELSE NULL END) AS avg_mfe_r, "
            "SUM(CASE WHEN status='closed' AND mae_r IS NOT NULL THEN 1 ELSE 0 END) AS mae_mfe_n, "
            "MIN(bar_date) AS first_date, MAX(bar_date) AS last_date "
            "FROM strategy_signals GROUP BY strategy_name ORDER BY strategy_name").fetchall()]
    by_name = {r["strategy_name"]: r for r in rows}

    # 依註冊表列出所有已登錄策略（即使尚無樣本也顯示，誠實呈現「尚未累積」），附研究假設。
    strat_list = []
    try:
        from . import load_builtin, registry as _reg
        load_builtin()
        strat_list = _reg.all_strategies()
    except Exception:
        strat_list = []

    def _status(closed, avg):
        if (closed or 0) < MIN_EVAL_SAMPLES:
            return {"state": "insufficient", "label": f"資料不足（已結算 {closed or 0} / 需 ≥{MIN_EVAL_SAMPLES}）"}
        if avg is None:
            return {"state": "insufficient", "label": "資料不足（無已結算淨 R）"}
        if avg > 0:
            return {"state": "tentative_pos", "label": "樣本達門檻、平均淨 R 為正（仍需顯著性檢定，非最終結論）"}
        return {"state": "tentative_neg", "label": "樣本達門檻、平均淨 R 未為正（未達標）"}

    def _mm(r):
        """逐筆 MAE/MFE 摘要（2026-11-12 起記錄）：只對有紀錄的已結算列平均；無紀錄＝None 不猜值。"""
        n = int(r.get("mae_mfe_n") or 0)
        f = lambda v: round(v, 3) if v is not None else None
        return {"avg_mae_r": f(r.get("avg_mae_r")) if n else None,
                "avg_mfe_r": f(r.get("avg_mfe_r")) if n else None, "mae_mfe_n": n}

    out = []
    seen = set()
    for s in strat_list:
        nm = getattr(s, "name", None)
        if not nm:
            continue
        seen.add(nm)
        r = by_name.get(nm, {})
        closed = int(r.get("closed") or 0)
        avg = r.get("avg_champion_r")
        avg = round(avg, 3) if avg is not None else None
        out.append({
            **_mm(r),
            "strategy_name": nm, "label": getattr(s, "label", nm), "direction": getattr(s, "direction", "both"),
            "total": int(r.get("total") or 0), "closed": closed,
            "open": int(r.get("total") or 0) - closed,
            "wins": int(r.get("wins") or 0), "losses": int(r.get("losses") or 0),
            "avg_champion_r": avg, "first_date": r.get("first_date"), "last_date": r.get("last_date"),
            "hypothesis": getattr(s, "hypothesis", None),
            "status": _status(closed, avg),
        })
    # 資料表裡有、但註冊表沒有的策略（歷史或已下架）也列出，不藏
    for nm, r in by_name.items():
        if nm in seen:
            continue
        closed = int(r.get("closed") or 0)
        avg = r.get("avg_champion_r")
        avg = round(avg, 3) if avg is not None else None
        out.append({
            **_mm(r),
            "strategy_name": nm, "label": nm, "direction": "unknown",
            "total": int(r.get("total") or 0), "closed": closed,
            "open": int(r.get("total") or 0) - closed,
            "wins": int(r.get("wins") or 0), "losses": int(r.get("losses") or 0),
            "avg_champion_r": avg, "first_date": r.get("first_date"), "last_date": r.get("last_date"),
            "hypothesis": None, "status": _status(closed, avg),
        })
    return {"strategies": out, "min_eval_samples": MIN_EVAL_SAMPLES,
            "gaps": ["回撤／換手／風險事件逐筆資料尚未逐欄記錄（需後端 schema，列為資料契約缺口）",
                     "逐筆 MAE/MFE 自 2026-11-12 起記錄；之前結算的舊列為 NULL（無紀錄，不回填）"],
            "note": "純 shadow 研究、未接線上、不授權切換正式策略；champion_r＝把各策略選股用 Champion "
                    "同一套分批出場規則結算的淨 R（費後：估列滑價/手續費/證交稅、放空含借券費）——"
                    "是『共同量尺』、非各策略自有出場邏輯的績效。mae_r/mfe_r＝持有窗內最不利／最有利"
                    "價格偏移（有號、R 計、以首根開盤為基準）——價格路徑極值、不含費稅，與 champion_r "
                    "口徑不同、不可相加比較。"}
