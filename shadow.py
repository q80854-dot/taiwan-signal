"""
影子追蹤（策略學習資料庫）。

為什麼需要：實際發出的訊號每天只有 0～2 檔，累積樣本太慢，無法回答「哪些條件真的有效」。
做法：每次掃描把「所有通過門檻的候選」（不論有沒有發出）連同當時的完整特徵存下來，
並以和實盤完全相同的停損／停利規則（含跳空以開盤價成交、同日先判停損）在背景追蹤結果。
另外隨機抽一批「沒有訊號的股票」當對照組，用同一套停損停利機制，回答「訊號到底有沒有比隨便買好」。

不會發通知、不會影響實際訊號，純粹累積資料。
"""
import json, logging, random, threading
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
TPE = timezone(timedelta(hours=8))
CONTROL_RATE = 0.03      # 被預判跳過的股票，抽 3% 當對照組（約 30～40 檔／天）
_lock = threading.Lock()
_ready = False

_COLS = ("bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,size_cat,sector,"
         "sent,features,status,created_at")


def _ddl():
    from state_store import USE_PG
    pk = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    return f"""
    CREATE TABLE IF NOT EXISTS shadow_signals (
        id {pk}, bar_date TEXT, ticker TEXT, code TEXT, name TEXT, kind TEXT, direction TEXT,
        score REAL, entry REAL, stop REAL, tp1 REAL, tp2 REAL, tp3 REAL, size_cat TEXT, sector TEXT,
        sent INTEGER DEFAULT 0, features TEXT, status TEXT DEFAULT 'pending', result TEXT,
        exit_price REAL, exit_date TEXT, r_multiple REAL, mfe_r REAL, mae_r REAL, bars_held INTEGER,
        gap_pct REAL, r5 REAL, r10 REAL, created_at TEXT,
        UNIQUE (bar_date, ticker)
    );
    CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_signals(status);
    CREATE INDEX IF NOT EXISTS idx_shadow_date ON shadow_signals(bar_date);
    """


def ensure_table():
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        from state_store import store
        with store._conn() as conn:
            conn.executescript(_ddl())
        _ready = True


def _scalar_features(sig, extra=None):
    f = {}
    for k, v in (sig or {}).items():
        if isinstance(v, bool):
            f[k] = int(v)
        elif isinstance(v, (int, float)) and v == v:
            f[k] = v
        elif isinstance(v, str) and len(v) <= 40 and k not in ("reason_full", "reason_brief", "action"):
            f[k] = v
    f.update(extra or {})
    return f


def _fund_features(code):
    try:
        from fund_score import build_fundamental_profile
        p = build_fundamental_profile(code)
        parts = p.get("parts") or {}
        rv = p.get("revenue") or {}
        return {"f_score": p.get("score"), "f_grade": p.get("grade"), "f_cov": p.get("coverage"),
                "f_growth": (parts.get("成長") or {}).get("score"), "f_profit": (parts.get("獲利") or {}).get("score"),
                "f_value": (parts.get("估值") or {}).get("score"), "rev_yoy": rv.get("yoy_pct")}
    except Exception:
        return {}


def _insert(rows):
    if not rows:
        return 0
    ensure_table()
    from state_store import store
    n = 0
    with store._conn() as conn:
        for r in rows:
            try:
                conn.execute(
                    f"INSERT INTO shadow_signals ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (bar_date, ticker) DO NOTHING",
                    (r["bar_date"], r["ticker"], r["code"], r["name"], r["kind"], r["direction"], r["score"],
                     r["entry"], r["stop"], r["tp1"], r["tp2"], r["tp3"], r["size_cat"], r["sector"],
                     0, json.dumps(r["features"], ensure_ascii=False), "pending", r["created_at"]))
                n += 1
            except Exception as e:
                logger.warning(f"shadow insert {r.get('ticker')}: {e}")
    return n


def _last_bar_date(ticker):
    from state_store import store
    return store.get_ohlcv_last_date(ticker, "daily")


def capture_candidates(signals, market_overview):
    """掃描結束、尚未過濾前呼叫：存下所有候選（含特徵）。"""
    try:
        now = datetime.now(timezone.utc).isoformat()
        sent_ = (market_overview or {}).get("sentiment_score")
        rows = []
        for s in signals or []:
            if not s.get("ticker") or not s.get("stop_loss"):
                continue
            bd = _last_bar_date(s["ticker"])
            if not bd:
                continue
            ff = _fund_features(s.get("code") or s["ticker"].split(".")[0])
            rows.append({"bar_date": bd[:10], "ticker": s["ticker"], "code": s.get("code"), "name": s.get("name"),
                         "kind": "candidate", "direction": s.get("direction"), "score": s.get("score"),
                         "entry": s.get("entry_price"), "stop": s.get("stop_loss"), "tp1": s.get("tp1"),
                         "tp2": s.get("tp2"), "tp3": s.get("tp3"), "size_cat": s.get("size_cat"),
                         "sector": s.get("sector"), "created_at": now,
                         "features": _scalar_features(s, dict(ff, sentiment=sent_,
                                                              dow=datetime.now(TPE).weekday()))})
        n = _insert(rows)
        logger.info(f"shadow: 候選已記錄 {n}/{len(rows)} 筆")
        return n
    except Exception as e:
        logger.warning(f"shadow.capture_candidates 失敗（不影響掃描）: {e}")
        return 0


def maybe_control(ticker, stock_info, daily, pre, market_overview, sink, lock):
    """被預判跳過的股票，隨機抽樣建立「假設做多」的對照單（同一套停損停利機制）。"""
    try:
        if random.random() >= CONTROL_RATE:
            return
        from indicators import calc_atr
        from signal_engine import calc_stop_loss_tw, calc_take_profits_tw
        closes, highs, lows = daily["closes"], daily["highs"], daily["lows"]
        price = closes[-1]
        atr = calc_atr(highs, lows, closes)
        if not atr.get("valid") or price <= 0:
            return
        size = stock_info.get("size_cat") or "中型股"
        sl = calc_stop_loss_tw("buy", price, atr["value"], {}, size, low_5d=min(lows[-5:]))
        sl = sl[0] if isinstance(sl, tuple) else (sl.get("stop_loss") if isinstance(sl, dict) else sl)
        tp = calc_take_profits_tw("buy", price, sl, size)
        code = stock_info.get("code") or ticker.split(".")[0]
        ff = _fund_features(code)
        feats = dict(ff, pre_score=pre.get("score"), pre_direction=pre.get("direction"), atr_pct=atr.get("pct"),
                     sentiment=(market_overview or {}).get("sentiment_score"), dow=datetime.now(TPE).weekday())
        row = {"bar_date": daily["dates"][-1][:10], "ticker": ticker, "code": code, "name": stock_info.get("name"),
               "kind": "control", "direction": "buy", "score": pre.get("score"), "entry": price, "stop": round(sl, 2),
               "tp1": tp["tp1"], "tp2": tp["tp2"], "tp3": tp["tp3"], "size_cat": size, "sector": stock_info.get("sector"),
               "created_at": datetime.now(timezone.utc).isoformat(), "features": feats}
        with lock:
            sink.append(row)
    except Exception as e:
        logger.debug(f"shadow control {ticker}: {e}")


def flush_controls(rows):
    try:
        n = _insert(rows)
        logger.info(f"shadow: 對照組已記錄 {n}/{len(rows)} 筆")
    except Exception as e:
        logger.warning(f"shadow.flush_controls 失敗: {e}")


def mark_sent(tickers):
    try:
        if not tickers:
            return
        ensure_table()
        from state_store import store
        with store._conn() as conn:
            for t in tickers:
                conn.execute("UPDATE shadow_signals SET sent=1 WHERE ticker=? AND kind='candidate' AND bar_date="
                             "(SELECT MAX(bar_date) FROM shadow_signals WHERE ticker=? AND kind='candidate')", (t, t))
    except Exception as e:
        logger.warning(f"shadow.mark_sent 失敗: {e}")


# ───────────── 結算 ─────────────
def _simulate(row, bars, expire_days):
    """回傳 dict：result/exit_price/exit_date/bars_held/mfe/mae/gap/r5/r10（皆以 R 為單位）。
    規則與實盤 scanner._resolve_pending_signals 相同：只看訊號日之後的K棒；同日先判停損；
    跳空穿越停損以開盤價成交；逾期（日曆天 ≥ expire_days）以最新收盤價結算為 expired。"""
    entry, stop = row["entry"], row["stop"]
    buy = row["direction"] == "buy"
    risk = abs(entry - stop)
    if not entry or not risk:
        return None
    sgn = 1 if buy else -1
    after = [b for b in bars if b["bar_date"][:10] > row["bar_date"]]
    out = {"result": None, "exit_price": None, "exit_date": None, "bars_held": len(after),
           "mfe": None, "mae": None, "gap": None, "r5": None, "r10": None}
    if not after:
        return out
    out["gap"] = round((after[0]["open"] / entry - 1) * 100, 2) if after[0].get("open") else None
    if len(after) >= 5:
        out["r5"] = round(sgn * (after[4]["close"] - entry) / risk, 3)
    if len(after) >= 10:
        out["r10"] = round(sgn * (after[9]["close"] - entry) / risk, 3)
    mfe, mae = -9e9, 9e9
    for i, b in enumerate(after):
        hi, lo, op = b["high"], b["low"], b.get("open")
        fav = sgn * ((hi if buy else lo) - entry) / risk
        adv = sgn * ((lo if buy else hi) - entry) / risk
        mfe, mae = max(mfe, fav), min(mae, adv)
        sl_hit = lo <= stop if buy else hi >= stop
        t3 = row.get("tp3"); t2 = row.get("tp2"); t1 = row.get("tp1")
        h3 = bool(t3) and ((hi >= t3) if buy else (lo <= t3))
        h2 = bool(t2) and ((hi >= t2) if buy else (lo <= t2))
        h1 = bool(t1) and ((hi >= t1) if buy else (lo <= t1))
        if not (sl_hit or h1 or h2 or h3):
            continue
        if sl_hit:
            res, px = "sl", stop
            if op and ((buy and op < stop) or (not buy and op > stop)):
                px = op
        elif h3: res, px = "tp3", t3
        elif h2: res, px = "tp2", t2
        else:    res, px = "tp1", t1
        out.update(result=res, exit_price=px, exit_date=b["bar_date"][:10], bars_held=i + 1)
        break
    out["mfe"], out["mae"] = round(mfe, 3), round(mae, 3)
    if out["result"] is None:
        try:
            age = (datetime.now(TPE).replace(tzinfo=None) - datetime.strptime(row["bar_date"], "%Y-%m-%d")).days
        except Exception:
            age = 0
        if age >= expire_days:
            out.update(result="expired", exit_price=after[-1]["close"], exit_date=after[-1]["bar_date"][:10])
    return out


def resolve_pending(max_rows=6000):
    """掃描完成後呼叫：用資料庫已更新的日K快取結算（不對外連線，速度快）。"""
    try:
        ensure_table()
        from state_store import store
        from config import CIRCUIT_BREAKER
        expire_days = CIRCUIT_BREAKER.get("signal_expire_days", 15)
        cutoff = (datetime.now(TPE) - timedelta(days=30)).strftime("%Y-%m-%d")
        with store._conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM shadow_signals WHERE status='pending' OR (r10 IS NULL AND bar_date>=?) "
                "ORDER BY bar_date LIMIT ?", (cutoff, max_rows)).fetchall()]
        by_t = {}
        for r in rows:
            by_t.setdefault(r["ticker"], []).append(r)
        upd = closed = 0
        pending_updates = []
        for t, lst in by_t.items():
            bars = store.get_cached_ohlcv_bars(t, "daily", limit=90)
            if not bars:
                continue
            for r in lst:
                sim = _simulate(r, bars, expire_days)
                if not sim:
                    continue
                fields, vals = ["gap_pct=?", "r5=?", "r10=?", "mfe_r=?", "mae_r=?", "bars_held=?"], \
                               [sim["gap"], sim["r5"], sim["r10"], sim["mfe"], sim["mae"], sim["bars_held"]]
                if r["status"] == "pending" and sim["result"]:
                    risk = abs(r["entry"] - r["stop"]); sgn = 1 if r["direction"] == "buy" else -1
                    rm = round(sgn * (sim["exit_price"] - r["entry"]) / risk, 3)
                    fields += ["status='closed'", "result=?", "exit_price=?", "exit_date=?", "r_multiple=?"]
                    vals += [sim["result"], sim["exit_price"], sim["exit_date"], rm]
                    closed += 1
                pending_updates.append((f"UPDATE shadow_signals SET {', '.join(fields)} WHERE id=?", vals + [r["id"]]))
                upd += 1
        if pending_updates:
            with store._conn() as conn:
                for q, v in pending_updates:
                    conn.execute(q, v)
        logger.info(f"shadow: 結算完成，更新 {upd} 筆、新結案 {closed} 筆")
        return {"updated": upd, "closed": closed}
    except Exception as e:
        logger.warning(f"shadow.resolve_pending 失敗（不影響掃描）: {e}", exc_info=True)
        return {}
