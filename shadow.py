"""
影子追蹤（策略學習資料庫）。

為什麼需要：實際發出的訊號每天只有 0～2 檔，累積樣本太慢，無法回答「哪些條件真的有效」。
做法：每次掃描把下列三類「假想單」連同當時的完整特徵存下來，並以和實盤完全相同的
停損／停利規則（含跳空以開盤價成交、同日先判停損）在背景追蹤結果：
  candidate  所有通過全部規則的候選（不論最後有沒有發出）
  rejected   分數達門檻，但被某一條規則（大盤放空閘門、延伸過度、停損過寬…）擋下的訊號
             ——用來回答「這條規則到底有沒有幫到我，還是擋掉了賺錢的單」
  control    沒有訊號、隨機抽樣的股票（假設做多，同一套停損停利）——回答「訊號有沒有比隨便買好」

不會發通知、不會影響實際訊號，純粹累積資料。所有對外函式都吞掉例外，不會拖垮掃描。

效能：資料庫每次 store._conn() 都是一條新連線，所以這裡一律「批次」查詢與寫入
（結算時一次撈回所有股票的K棒，而不是一檔一次連線）。
"""
import csv, io, json, logging, random, threading, time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)
TPE = timezone(timedelta(hours=8))
CONTROL_RATE = 1.0         # 沒有訊號的股票抽樣比例。2026-10-06：原 3% 實測每次只抽到 1～7 檔（約 96% 的股票日線分數都≥55，
                           # 真正『沒有訊號』的只有少數），改成水庫抽樣：所有『沒有訊號』的股票等機率抽出 CONTROL_CAP 檔
CONTROL_CAP = 40
REJ_PER_REASON = 50        # 每個否決原因每次掃描最多存幾筆（依分數高到低）
REJ_TOTAL = 220            # 每次掃描被否決樣本總上限
_lock = threading.Lock()
_ready = False

# 否決原因代碼 → 中文（learning.py / 網頁共用）
REASONS = {
    "short_off": "放空功能關閉",
    "gate_regime": "放空：大盤不是明確中期空頭",
    "gate_shock": "放空：大盤單日情緒過高",
    "gate_weekly": "放空：週線不是空頭",
    "gate_adxdir": "放空：ADX 方向不是空方主導",
    "short_score": "放空：分數未達放空門檻",
    "adx_low": "ADX 趨勢強度不足",
    "vol_low": "量能不足",
    "macro_stop": "大盤／外資熔斷擋下多單",
    "score_after_adj": "總經扣分後未達門檻",
    "chase_limit": "當日接近漲跌停（追價風險）",
    "extension": "離 20 日線太遠（延伸過度）",
    "sl_wide": "停損距離過寬（>12%）",
    "rr_low": "盈虧比不足",
    "size_zero": "建議部位為 0",
}

_COLS = ("bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,size_cat,sector,"
         "sent,features,status,created_at")

# ───────────── 一次掃描的收集狀態 ─────────────
_run = None
_run_guard = threading.Lock()


def begin_run(note=""):
    """掃描開始時呼叫：之後 signal_engine 才會把被否決的訊號丟進來。"""
    global _run
    with _run_guard:
        _run = {"t0": time.time(), "note": note, "feats": {}, "rejects": [], "controls": [], "lock": threading.Lock(),
                "fund": {}, "stats": {"rej_seen": 0, "cand_found": 0, "cand_saved": 0, "rej_saved": 0,
                                      "ctrl_made": 0, "ctrl_saved": 0, "res_updated": 0, "res_closed": 0,
                                      "secs_capture": 0.0, "secs_resolve": 0.0}, "errors": [], "bar_date": None}


def _err(msg):
    r = _run
    if r is not None and len(r["errors"]) < 10:
        r["errors"].append(str(msg)[:200])


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
    CREATE INDEX IF NOT EXISTS idx_shadow_kind ON shadow_signals(kind, status);
    CREATE TABLE IF NOT EXISTS shadow_runs (
        id {pk}, run_at TEXT, bar_date TEXT, note TEXT, universe INTEGER, pre_skipped INTEGER, pre_passed INTEGER,
        cand_found INTEGER, cand_saved INTEGER, rej_seen INTEGER, rej_saved INTEGER, ctrl_made INTEGER,
        ctrl_saved INTEGER, res_updated INTEGER, res_closed INTEGER, secs_capture REAL, secs_resolve REAL,
        secs_total REAL, errors TEXT
    );
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


# ───────────── 特徵 ─────────────
_DROP = {"id", "ticker", "code", "name", "sector", "emoji", "emoji_grade", "direction_zh", "action",
         "reason_full", "reason_brief", "generated_at", "status", "result", "exit_plan", "short_research_only_reason"}


def _scalar_features(sig):
    f = {}
    for k, v in (sig or {}).items():
        if k in _DROP:
            continue
        if isinstance(v, bool):
            f[k] = int(v)
        elif isinstance(v, (int, float)) and v == v:
            f[k] = v
        elif isinstance(v, str) and len(v) <= 40:
            f[k] = v
    return f


def _pct(a, b):
    try:
        return round((a / b - 1) * 100, 2) if a and b else None
    except Exception:
        return None


def snapshot(daily, ind=None, mtf=None, mkt=None, atr_val=None):
    """把一檔股票「當下」的可量測特徵攤平成純量字典（價格動能、位置、量能、波動、大盤環境）。
    候選／被否決／對照組都用同一支函式，之後才能互相比較。"""
    c = daily.get("closes") or []
    if not c:
        return {}
    h, l, o, v, d = (daily.get(k) or [] for k in ("highs", "lows", "opens", "volumes", "dates"))
    n, px = len(c), c[-1]
    f = {"_bd": d[-1][:10] if d else None, "hist_days": n, "chg_pct": daily.get("change_pct")}
    if n > 1 and o:
        f["gap_today"] = _pct(o[-1], c[-2])
    if h and l and px:
        f["range_pct"] = round((h[-1] - l[-1]) / px * 100, 2)
    for k in (5, 10, 20, 60):
        if n > k:
            f[f"ret{k}"] = _pct(px, c[-1 - k])
    if h:
        f["dist_hi52"] = _pct(px, max(h[-250:]))
    if l:
        f["dist_lo52"] = _pct(px, min(l[-250:]))
    if n >= 20:
        f["dist_ma20"] = _pct(px, sum(c[-20:]) / 20)
        rets = [(c[i] / c[i - 1] - 1) * 100 for i in range(n - 19, n) if c[i - 1]]
        if len(rets) > 5:
            m = sum(rets) / len(rets)
            f["vol20"] = round((sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5, 2)
    if n >= 60:
        f["dist_ma60"] = _pct(px, sum(c[-60:]) / 60)
    if len(v) >= 21:
        avg = sum(v[-21:-1]) / 20
        if avg:
            f["vol_x20"] = round(v[-1] / avg, 2)
        f["vol_last"] = v[-1]
    atr = atr_val or ((ind or {}).get("atr") or {}).get("value")
    if atr and px:
        f["atr_pct"] = round(atr / px * 100, 2)
    if ind:
        ema = ind.get("ema") or {}
        if ema.get("alignment"):
            f["ema_align"] = str(ema["alignment"])[:30]
        if (ind.get("macd") or {}).get("bias"):
            f["macd_bias"] = str(ind["macd"]["bias"])[:30]
    if mtf:
        for k in ("score", "bull_score", "bear_score", "adx_value", "rsi_value", "vol_ratio", "weekly_bias",
                  "adx_bias", "resonance"):
            val = mtf.get(k)
            if isinstance(val, bool):
                val = int(val)
            if val is not None:
                f["raw_score" if k == "score" else k] = val
    if mkt:
        f["sentiment"] = mkt.get("sentiment_score")
        reg = mkt.get("regime") or {}
        if reg.get("trend"):
            f["regime"] = str(reg["trend"])[:20]
        f["regime_bear"] = int(bool(reg.get("bearish_regime")))
        i = 0
        for k, val in mkt.items():
            if i >= 10:
                break
            if isinstance(val, (int, float)) and not isinstance(val, bool) and val == val and "mkt_" + k not in f:
                f["mkt_" + k] = val
                i += 1
    now = datetime.now(TPE)
    f["dow"], f["month"] = now.weekday(), now.month
    return {k: val for k, val in f.items() if val is not None}


def observe(ticker, daily, ind, mtf, mkt):
    """signal_engine 在『分數達門檻』時呼叫，先記下這檔的特徵快照。"""
    r = _run
    if r is None:
        return
    try:
        r["feats"][ticker] = snapshot(daily, ind, mtf, mkt)
    except Exception as e:
        _err(f"observe {ticker}: {e}")


def _fund_features(code, cache=None):
    if cache is not None and code in cache:
        return cache[code]
    out = {}
    try:
        from fund_score import build_fundamental_profile
        p = build_fundamental_profile(code)
        parts = p.get("parts") or {}
        rv = p.get("revenue") or {}
        out = {"f_score": p.get("score"), "f_grade": p.get("grade"), "f_cov": p.get("coverage"),
               "f_growth": (parts.get("成長") or {}).get("score"), "f_profit": (parts.get("獲利") or {}).get("score"),
               "f_value": (parts.get("估值") or {}).get("score"), "rev_yoy": rv.get("yoy_pct")}
        out = {k: v for k, v in out.items() if v is not None}
    except Exception:
        out = {}
    if cache is not None:
        cache[code] = out
    return out


# ───────────── 寫入 ─────────────
def _insert(rows):
    """一條連線批次寫入；回傳實際新增筆數（重複的 (日期,股票) 會被略過）。"""
    if not rows:
        return 0
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        before = (conn.execute("SELECT COUNT(*) AS n FROM shadow_signals").fetchone() or {"n": 0})["n"]
        for r in rows:
            try:
                conn.execute(
                    f"INSERT INTO shadow_signals ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (bar_date, ticker) DO NOTHING",
                    (r["bar_date"], r["ticker"], r["code"], r["name"], r["kind"], r["direction"], r["score"],
                     r["entry"], r["stop"], r["tp1"], r["tp2"], r["tp3"], r["size_cat"], r["sector"],
                     0, json.dumps(r["features"], ensure_ascii=False), "pending", r["created_at"]))
            except Exception as e:
                logger.warning(f"shadow insert {r.get('ticker')}: {e}")
                _err(f"insert {r.get('ticker')}: {e}")
        after = (conn.execute("SELECT COUNT(*) AS n FROM shadow_signals").fetchone() or {"n": 0})["n"]
    return max(0, after - before)


def _last_bar_date(ticker):
    from state_store import store
    return store.get_ohlcv_last_date(ticker, "daily")


def _as_float(sl):
    if isinstance(sl, tuple):
        sl = sl[0]
    if isinstance(sl, dict):
        sl = sl.get("stop_loss")
    return float(sl) if sl is not None else None


def capture_candidates(signals, market_overview):
    """掃描結束、尚未過濾前呼叫：存下所有候選（含完整特徵）。"""
    t0 = time.time()
    r = _run
    try:
        now = datetime.now(timezone.utc).isoformat()
        cache = r["fund"] if r is not None else {}
        rows = []
        for s in signals or []:
            if not s.get("ticker") or not s.get("stop_loss"):
                continue
            snap = dict((r["feats"].get(s["ticker"]) if r is not None else None) or {})
            bd = snap.pop("_bd", None) or (_last_bar_date(s["ticker"]) or "")[:10]
            if not bd:
                continue
            feats = dict(snap)
            feats.update(_scalar_features(s))
            feats.update(_fund_features(s.get("code") or s["ticker"].split(".")[0], cache))
            rows.append({"bar_date": bd, "ticker": s["ticker"], "code": s.get("code"), "name": s.get("name"),
                         "kind": "candidate", "direction": s.get("direction"), "score": s.get("score"),
                         "entry": s.get("entry_price"), "stop": s.get("stop_loss"), "tp1": s.get("tp1"),
                         "tp2": s.get("tp2"), "tp3": s.get("tp3"), "size_cat": s.get("size_cat"),
                         "sector": s.get("sector"), "created_at": now, "features": feats})
        n = _insert(rows)
        logger.info(f"shadow: 候選已記錄 {n}/{len(rows)} 筆（其餘為當日已記錄過）")
        if r is not None:
            r["stats"]["cand_found"] += len(rows)
            r["stats"]["cand_saved"] += n
            if rows:
                r["bar_date"] = rows[-1]["bar_date"]
        return n
    except Exception as e:
        logger.warning(f"shadow.capture_candidates 失敗（不影響掃描）: {e}")
        _err(f"capture_candidates: {e}")
        return 0
    finally:
        if r is not None:
            r["stats"]["secs_capture"] += time.time() - t0


def note_reject(ticker, stock_info, daily, ind, mtf, direction, score, reason, mkt=None, sl=None):
    """signal_engine 在『分數已達門檻、卻被某條規則擋下』時呼叫，記一筆假想單。"""
    r = _run
    if r is None:
        return
    try:
        with r["lock"]:
            r["stats"]["rej_seen"] += 1
        closes = daily.get("closes") or []
        px = closes[-1] if closes else 0
        atr = ((ind or {}).get("atr") or {}).get("value")
        if px <= 0 or not atr or direction not in ("buy", "sell"):
            return
        from signal_engine import calc_stop_loss_tw, calc_take_profits_tw
        size = (stock_info or {}).get("size_cat") or "中型股"
        lows, highs = daily.get("lows") or [], daily.get("highs") or []
        if sl is None:
            sl = _as_float(calc_stop_loss_tw(direction, px, atr, ind, size,
                                             min(lows[-5:]) if len(lows) >= 5 else None,
                                             max(highs[-5:]) if len(highs) >= 5 else None))
        if sl is None or sl <= 0 or (direction == "buy" and sl >= px) or (direction == "sell" and sl <= px):
            return
        tp = calc_take_profits_tw(direction, px, sl, size)
        snap = dict(r["feats"].get(ticker) or snapshot(daily, ind, mtf, mkt))
        bd = snap.pop("_bd", None) or ((daily.get("dates") or [""])[-1] or "")[:10]
        if not bd:
            return
        snap.update(reject=reason, sl_atr=round(abs(px - sl) / atr, 2), sl_pct=round(abs(px - sl) / px * 100, 2))
        code = (stock_info or {}).get("code") or ticker.split(".")[0]
        row = {"bar_date": bd, "ticker": ticker, "code": code, "name": (stock_info or {}).get("name"),
               "kind": "rejected", "direction": direction, "score": score, "entry": px, "stop": round(sl, 2),
               "tp1": tp["tp1"], "tp2": tp["tp2"], "tp3": tp["tp3"], "size_cat": size,
               "sector": (stock_info or {}).get("sector"),
               "created_at": datetime.now(timezone.utc).isoformat(), "features": snap}
        with r["lock"]:
            r["rejects"].append(row)
    except Exception as e:
        _err(f"note_reject {ticker}: {e}")


def flush_rejects():
    """掃描結束時呼叫：依分數挑出每個原因最高分的樣本（有上限，避免資料爆量），補基本面後寫入。"""
    t0 = time.time()
    r = _run
    if r is None:
        return 0
    try:
        by = {}
        for x in r["rejects"]:
            by.setdefault(x["features"].get("reject"), []).append(x)
        keep = []
        for lst in by.values():
            lst.sort(key=lambda z: -(z.get("score") or 0))
            keep += lst[:REJ_PER_REASON]
        keep.sort(key=lambda z: -(z.get("score") or 0))
        keep = keep[:REJ_TOTAL]
        for x in keep:
            x["features"].update(_fund_features(x["code"], r["fund"]))
        n = _insert(keep)
        logger.info(f"shadow: 被規則擋下的樣本已記錄 {n}/{len(keep)} 筆（本次共遇到 {r['stats']['rej_seen']} 筆，"
                    f"依原因分數取前 {REJ_PER_REASON}）")
        r["stats"]["rej_saved"] += n
        return n
    except Exception as e:
        logger.warning(f"shadow.flush_rejects 失敗: {e}")
        _err(f"flush_rejects: {e}")
        return 0
    finally:
        r["stats"]["secs_capture"] += time.time() - t0


def maybe_control(ticker, stock_info, daily, pre, market_overview, sink, lock):
    """被預判跳過的股票，隨機抽樣建立「假設做多」的對照單（同一套停損停利機制）。"""
    try:
        if random.random() >= CONTROL_RATE:
            return
        with lock:
            if len(sink) >= CONTROL_CAP:
                return
        from indicators import calc_atr
        from signal_engine import calc_stop_loss_tw, calc_take_profits_tw
        closes, highs, lows = daily["closes"], daily["highs"], daily["lows"]
        price = closes[-1]
        atr = calc_atr(highs, lows, closes)
        if not atr.get("valid") or price <= 0:
            return
        size = stock_info.get("size_cat") or "中型股"
        sl = _as_float(calc_stop_loss_tw("buy", price, atr["value"], {}, size, low_5d=min(lows[-5:])))
        tp = calc_take_profits_tw("buy", price, sl, size)
        code = stock_info.get("code") or ticker.split(".")[0]
        snap = snapshot(daily, None, pre, market_overview, atr_val=atr["value"])
        bd = snap.pop("_bd", None) or daily["dates"][-1][:10]
        snap.update(pre_score=pre.get("score"), pre_direction=pre.get("direction"))
        row = {"bar_date": bd, "ticker": ticker, "code": code, "name": stock_info.get("name"),
               "kind": "control", "direction": "buy", "score": pre.get("score"), "entry": price, "stop": round(sl, 2),
               "tp1": tp["tp1"], "tp2": tp["tp2"], "tp3": tp["tp3"], "size_cat": size, "sector": stock_info.get("sector"),
               "created_at": datetime.now(timezone.utc).isoformat(), "features": snap}
        with lock:
            sink.append(row)
    except Exception as e:
        logger.debug(f"shadow control {ticker}: {e}")


def control_from_engine(ticker, stock_info, daily, ind, mtf, mkt):
    """signal_engine 判定『這檔沒有訊號』時呼叫：隨機抽 CONTROL_RATE 當對照組（假設做多，同一套停損停利）。
    （日線預篩實際只跳過約 4% 的股票，所以對照組主要從這裡抽，樣本才夠。）"""
    r = _run
    if r is None:
        return
    # 水庫抽樣（Reservoir sampling）：掃描順序固定，若「先到先收」會偏向代號小的股票；
    # 這樣每一檔沒訊號的股票被選進對照組的機率完全相同。
    with r["lock"]:
        r["ctrl_seen"] = r.get("ctrl_seen", 0) + 1
        seen = r["ctrl_seen"]
        if len(r["controls"]) < CONTROL_CAP:
            slot = len(r["controls"])
        else:
            j = random.randrange(seen)
            if j >= CONTROL_CAP:
                return
            slot = j
    try:
        from signal_engine import calc_stop_loss_tw, calc_take_profits_tw
        closes = daily.get("closes") or []
        px = closes[-1] if closes else 0
        atr = ((ind or {}).get("atr") or {}).get("value")
        if px <= 0 or not atr:
            return
        size = (stock_info or {}).get("size_cat") or "中型股"
        lows = daily.get("lows") or []
        sl = _as_float(calc_stop_loss_tw("buy", px, atr, ind, size, min(lows[-5:]) if len(lows) >= 5 else None, None))
        if sl is None or sl <= 0 or sl >= px:
            return
        tp = calc_take_profits_tw("buy", px, sl, size)
        snap = snapshot(daily, ind, mtf, mkt)
        bd = snap.pop("_bd", None) or ((daily.get("dates") or [""])[-1] or "")[:10]
        if not bd:
            return
        snap.update(pre_score=(mtf or {}).get("score"), pre_direction=(mtf or {}).get("direction"))
        code = (stock_info or {}).get("code") or ticker.split(".")[0]
        row = {"bar_date": bd, "ticker": ticker, "code": code, "name": (stock_info or {}).get("name"),
               "kind": "control", "direction": "buy", "score": (mtf or {}).get("score"), "entry": px,
               "stop": round(sl, 2), "tp1": tp["tp1"], "tp2": tp["tp2"], "tp3": tp["tp3"], "size_cat": size,
               "sector": (stock_info or {}).get("sector"),
               "created_at": datetime.now(timezone.utc).isoformat(), "features": snap}
        with r["lock"]:
            if slot < len(r["controls"]):
                r["controls"][slot] = row
            else:
                r["controls"].append(row)
    except Exception as e:
        _err(f"control_from_engine {ticker}: {e}")


def flush_controls(rows):
    t0 = time.time()
    r = _run
    try:
        cache = r["fund"] if r is not None else {}
        rows = list(rows or [])
        if r is not None:
            seen = {x["ticker"] for x in rows}
            rows += [x for x in r["controls"] if x["ticker"] not in seen]
        for x in rows:
            x["features"].update(_fund_features(x["code"], cache))
        n = _insert(rows)
        logger.info(f"shadow: 對照組已記錄 {n}/{len(rows)} 筆")
        if r is not None:
            r["stats"]["ctrl_made"] += len(rows)
            r["stats"]["ctrl_saved"] += n
    except Exception as e:
        logger.warning(f"shadow.flush_controls 失敗: {e}")
        _err(f"flush_controls: {e}")
    finally:
        if r is not None:
            r["stats"]["secs_capture"] += time.time() - t0


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


def _load_bars(tickers, since):
    """一條連線、分批 IN 查詢，撈回所有需要的日K（取代「一檔一條連線」）。"""
    from state_store import store
    out = {}
    with store._conn() as conn:
        for i in range(0, len(tickers), 250):
            chunk = tickers[i:i + 250]
            ph = ",".join("?" * len(chunk))
            rs = conn.execute(
                f"SELECT ticker,bar_date,open,high,low,close FROM ohlcv_bars WHERE tf_key='daily' "
                f"AND ticker IN ({ph}) AND bar_date>=? ORDER BY ticker,bar_date", tuple(chunk) + (since,)).fetchall()
            for x in rs:
                out.setdefault(x["ticker"], []).append(dict(x))
    return out


def _rescale(row, bars):
    """Yahoo 日K是『還原價』，除權息後整段歷史會被重新調整；而我們存的進場／停損／停利是當時的未還原價。
    以『訊號日那根K棒的現在收盤 ÷ 當時進場價』當比例，把四個價位一起縮放後再模擬，R 值不受影響。"""
    sig = next((b for b in bars if b["bar_date"][:10] == row["bar_date"]), None)
    if not sig or not row.get("entry") or not sig.get("close"):
        return row, 1.0
    s = sig["close"] / row["entry"]
    if 0.003 < abs(s - 1) < 0.4:
        r2 = dict(row)
        for k in ("entry", "stop", "tp1", "tp2", "tp3"):
            if r2.get(k):
                r2[k] = r2[k] * s
        return r2, s
    return row, 1.0


def _same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-6)


def resolve_pending(max_rows=8000):
    """掃描完成後呼叫：用資料庫已更新的日K快取結算（不對外連線）。
    只寫入『有變動』的列，並把 r5/r10 補齊給近 30 天的列。"""
    t0 = time.time()
    r = _run
    res = {}
    try:
        ensure_table()
        from state_store import store
        from config import CIRCUIT_BREAKER
        expire_days = CIRCUIT_BREAKER.get("signal_expire_days", 15)
        cutoff = (datetime.now(TPE) - timedelta(days=30)).strftime("%Y-%m-%d")
        with store._conn() as conn:
            rows = [dict(x) for x in conn.execute(
                "SELECT * FROM shadow_signals WHERE status='pending' OR (r10 IS NULL AND bar_date>=?) "
                "ORDER BY bar_date LIMIT ?", (cutoff, max_rows)).fetchall()]
        if not rows:
            logger.info("shadow: 結算完成，沒有需要處理的紀錄")
            return {"updated": 0, "closed": 0, "rows": 0}
        since = (datetime.strptime(min(x["bar_date"] for x in rows), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
        bars_by = _load_bars(sorted({x["ticker"] for x in rows}), since)
        updates, closed, rescaled = [], 0, 0
        for row in rows:
            bars = bars_by.get(row["ticker"])
            if not bars:
                continue
            r2, scale = _rescale(row, bars)
            rescaled += scale != 1.0
            sim = _simulate(r2, bars, expire_days)
            if not sim:
                continue
            new = {"gap_pct": sim["gap"], "r5": sim["r5"], "r10": sim["r10"], "mfe_r": sim["mfe"],
                   "mae_r": sim["mae"], "bars_held": sim["bars_held"]}
            newly_closed = row["status"] == "pending" and sim["result"]
            changed = newly_closed or any(not _same(row.get(k), v) for k, v in new.items())
            if not changed:
                continue
            fields, vals = [f"{k}=?" for k in new], list(new.values())
            if newly_closed:
                risk = abs(r2["entry"] - r2["stop"]); sgn = 1 if row["direction"] == "buy" else -1
                fields += ["status='closed'", "result=?", "exit_price=?", "exit_date=?", "r_multiple=?"]
                vals += [sim["result"], sim["exit_price"], sim["exit_date"], round(sgn * (sim["exit_price"] - r2["entry"]) / risk, 3)]
                closed += 1
            updates.append((f"UPDATE shadow_signals SET {', '.join(fields)} WHERE id=?", vals + [row["id"]]))
        if updates:
            with store._conn() as conn:
                for q, v in updates:
                    conn.execute(q, v)
        res = {"updated": len(updates), "closed": closed, "rows": len(rows), "rescaled": int(rescaled)}
        logger.info(f"shadow: 結算完成，檢查 {len(rows)} 筆、更新 {len(updates)} 筆、新結案 {closed} 筆"
                    f"（除權息還原調整 {int(rescaled)} 筆），耗時 {time.time() - t0:.1f}s")
        if r is not None:
            r["stats"]["res_updated"] += len(updates)
            r["stats"]["res_closed"] += closed
        return res
    except Exception as e:
        logger.warning(f"shadow.resolve_pending 失敗（不影響掃描）: {e}", exc_info=True)
        _err(f"resolve_pending: {e}")
        return res
    finally:
        if r is not None:
            r["stats"]["secs_resolve"] += time.time() - t0


def finish_run(universe=None, pre_skipped=None, pre_passed=None):
    """整次掃描收尾：把這次收集了多少、花多久、有沒有錯誤寫進 shadow_runs（後台『收集紀錄』）。"""
    global _run
    r, _run = _run, None
    if r is None:
        return
    try:
        ensure_table()
        from state_store import store
        s = r["stats"]
        with store._conn() as conn:
            conn.execute(
                "INSERT INTO shadow_runs (run_at,bar_date,note,universe,pre_skipped,pre_passed,cand_found,cand_saved,"
                "rej_seen,rej_saved,ctrl_made,ctrl_saved,res_updated,res_closed,secs_capture,secs_resolve,secs_total,errors) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), r["bar_date"], r["note"], universe, pre_skipped, pre_passed,
                 s["cand_found"], s["cand_saved"], s["rej_seen"], s["rej_saved"], s["ctrl_made"], s["ctrl_saved"],
                 s["res_updated"], s["res_closed"], round(s["secs_capture"], 2), round(s["secs_resolve"], 2),
                 round(time.time() - r["t0"], 1), json.dumps(r["errors"], ensure_ascii=False) if r["errors"] else None))
    except Exception as e:
        logger.warning(f"shadow.finish_run 失敗: {e}")


# ───────────── 後台：總覽與匯出 ─────────────
def overview():
    """給管理後台：資料量、各日收集量、每次掃描的收集紀錄、特徵覆蓋率、警示。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        kinds = [dict(x) for x in conn.execute(
            "SELECT kind,status,COUNT(*) AS n FROM shadow_signals GROUP BY kind,status").fetchall()]
        per_day = [dict(x) for x in conn.execute(
            "SELECT bar_date,kind,COUNT(*) AS n FROM shadow_signals GROUP BY bar_date,kind "
            "ORDER BY bar_date DESC LIMIT 90").fetchall()]
        runs = [dict(x) for x in conn.execute("SELECT * FROM shadow_runs ORDER BY id DESC LIMIT 20").fetchall()]
        meta = dict(conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(LENGTH(features)),0) AS feat_bytes, MIN(bar_date) AS first_d, "
            "MAX(bar_date) AS last_d, MAX(created_at) AS last_created FROM shadow_signals").fetchone() or {})
        _o = conn.execute("SELECT MIN(bar_date) AS d FROM shadow_signals WHERE status='pending'").fetchone()
        oldest = dict(_o).get("d") if _o else None
        sample = [dict(x) for x in conn.execute(
            "SELECT kind,features FROM shadow_signals ORDER BY id DESC LIMIT 600").fetchall()]
        recent = [dict(x) for x in conn.execute(
            "SELECT bar_date,ticker,name,kind,direction,score,entry,stop,status,result,r_multiple,features "
            "FROM shadow_signals ORDER BY id DESC LIMIT 40").fetchall()]
    # 特徵覆蓋率（每種來源各自算）
    cov, cnt = {}, {}
    for x in sample:
        try:
            f = json.loads(x["features"] or "{}")
        except Exception:
            f = {}
        cnt[x["kind"]] = cnt.get(x["kind"], 0) + 1
        for k in f:
            cov.setdefault(k, {}).setdefault(x["kind"], 0)
            cov[k][x["kind"]] += 1
    coverage = []
    for k, d in cov.items():
        coverage.append({"key": k, "pct": {kd: round(d.get(kd, 0) / cnt[kd] * 100) for kd in cnt}})
    coverage.sort(key=lambda z: -max(z["pct"].values()))
    for x in recent:
        try:
            f = json.loads(x.pop("features") or "{}")
        except Exception:
            f = {}
        x["reject"] = REASONS.get(f.get("reject"), f.get("reject"))
    summary = {}
    for k in kinds:
        d = summary.setdefault(k["kind"], {"pending": 0, "closed": 0})
        d[k["status"] or "pending"] = d.get(k["status"] or "pending", 0) + k["n"]
    alerts = []
    try:
        today = datetime.now(TPE).date()
        if meta.get("last_d"):
            lag = (today - datetime.strptime(meta["last_d"], "%Y-%m-%d").date()).days
            if lag >= 4:
                alerts.append(f"最近一筆資料是 {meta['last_d']}（{lag} 天前），請確認掃描是否正常執行")
        else:
            alerts.append("資料庫還沒有任何影子追蹤紀錄（第一次完整掃描後才會出現）")
        if oldest and (today - datetime.strptime(oldest, "%Y-%m-%d").date()).days > expire_guard():
            alerts.append(f"有追蹤中的紀錄從 {oldest} 起仍未結算，超過有效期，可能結算流程卡住")
        if runs and runs[0].get("errors"):
            alerts.append("最近一次掃描的影子追蹤有錯誤，詳見下方收集紀錄")
        if runs and (runs[0].get("cand_found") or 0) and not (runs[0].get("cand_saved") or 0):
            alerts.append("最近一次掃描找到候選卻沒有新增寫入（可能是同一天重複掃描，已被去重）")
    except Exception:
        pass
    return {"summary": summary, "per_day": per_day, "runs": runs, "meta": meta, "oldest_pending": oldest,
            "coverage": coverage[:60], "coverage_n": cnt, "recent": recent, "alerts": alerts,
            "reasons": REASONS, "control_rate": CONTROL_RATE,
            "caps": {"per_reason": REJ_PER_REASON, "total": REJ_TOTAL}}


def expire_guard():
    try:
        from config import CIRCUIT_BREAKER
        return int(CIRCUIT_BREAKER.get("signal_expire_days", 15)) + 10
    except Exception:
        return 25


def export_csv(kind=None, status=None):
    """匯出成 CSV（特徵攤平成欄位，UTF-8 BOM 方便 Excel 開啟）。供之後離線做大數據分析。"""
    ensure_table()
    from state_store import store
    q, args = "SELECT * FROM shadow_signals", []
    cond = []
    if kind:
        cond.append("kind=?"); args.append(kind)
    if status:
        cond.append("status=?"); args.append(status)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY bar_date,id"
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(q, tuple(args)).fetchall()]
    keys, parsed = {}, []
    for x in rows:
        try:
            f = json.loads(x.pop("features") or "{}")
        except Exception:
            f = {}
        parsed.append(f)
        for k in f:
            keys[k] = keys.get(k, 0) + 1
    fkeys = [k for k, _ in sorted(keys.items(), key=lambda z: -z[1])][:150]
    base = ["id", "bar_date", "ticker", "code", "name", "kind", "direction", "score", "entry", "stop", "tp1", "tp2",
            "tp3", "size_cat", "sector", "sent", "status", "result", "exit_price", "exit_date", "r_multiple", "mfe_r",
            "mae_r", "bars_held", "gap_pct", "r5", "r10", "created_at"]
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(base + ["f_" + k for k in fkeys])
    for x, f in zip(rows, parsed):
        w.writerow([x.get(k) for k in base] + [f.get(k) for k in fkeys])
    return out.getvalue(), len(rows)
