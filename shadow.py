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
# ★ 2026-11-12（S-P1-04）配對對照組：每檔做多候選配 MATCH_K 檔「同日、同產業、同規模、成交金額最接近」的
# 非訊號股。7 年重播（docs/research/MATCHED_AND_BOOTSTRAP_2020_2026.md）顯示：拿全體非訊號股（現行隨機對照）
# 當基準，會把訊號的 20 日超額報酬高估約 0.44 個百分點——一部分「優勢」只是產業與規模組成不同。
MATCH_K = 3
REJ_PER_REASON = 50        # 每個否決原因每次掃描最多存幾筆（依分數高到低）
REJ_TOTAL = 220            # 每次掃描被否決樣本總上限
_lock = threading.Lock()
_ready = False

# ★ 2026-11-06（P0 資料可信度）：MAE/MFE/R 爆量防護。
# 根因：R 以「進場價−停損」(risk) 為分母。當 entry≈stop（浮點微差，risk≈1e-7，多半出現在
# control 抽樣或退化資料）時，(Δ價/risk) 會炸到 ~1e9，污染 mae_r/mfe_r/r_multiple，進而污染
# learning 標籤與績效統計（CSV 快照裡的 ±8,999,999,000 即源於此，非 -9e9 初始哨兵）。
#   · _MIN_RISK_FRAC：risk 至少要是進場價的這個比例，否則視為退化、不產 R（真實波段停損約 2~8%，
#     0.05% 只會擋到退化列，不影響正常訊號）。
#   · _ABSURD_R：|R| 超過此界即非物理值（真實波段 R 幾乎都落在 [-3, +10]），用於清洗既有髒列。
_MIN_RISK_FRAC = 0.0005
_ABSURD_R = 200.0
_R_COLS = ("r_multiple", "mae_r", "mfe_r", "r1", "r5", "r10", "r20")


def _risk_ok(entry, stop) -> bool:
    """風險分母是否可用：entry、stop 皆為有效正數，且 |entry−stop| ≥ entry×_MIN_RISK_FRAC。
    不可用代表 entry≈stop（退化資料），以此為分母算出的 R 無意義。"""
    try:
        e, s = float(entry), float(stop)
    except (TypeError, ValueError):
        return False
    if not (e > 0) or not (s > 0):
        return False
    return abs(e - s) >= e * _MIN_RISK_FRAC

# 否決原因代碼 → 中文（learning.py / 網頁共用）
REASONS = {
    "short_off": "放空已停發（通過全部放空關卡的假想單）",
    "gate_weekly_long": "做多：週線不是偏多",
    "long_score": "做多：最終分數未達 75",
    "gate_regime": "放空：大盤不是明確中期空頭",
    "gate_shock": "放空：大盤單日情緒過高",
    "gate_weekly": "放空：週線不是空頭",
    "gate_adxdir": "放空：ADX 方向不是空方主導",
    "short_score": "放空：分數未達放空門檻",
    "short_odd_lot": "放空：部位不足 1 張（零股不能放空）",
    "adx_low": "ADX 趨勢強度不足",
    "vol_low": "量能不足",
    "macro_stop": "大盤／外資熔斷擋下多單",
    "market_stop": "大盤重挫日暫停所有新訊號（多空皆停）",
    "score_after_adj": "總經扣分後未達門檻",
    "chase_limit": "當日接近漲跌停（追價風險）",
    "extension": "離 20 日線太遠（延伸過度）",
    "sl_wide": "停損距離過寬（>12%）",
    "rr_low": "盈虧比不足",
    "size_zero": "建議部位為 0",
    "exdiv": "近日除權息（股價將跳空）",
    "material_news": "命中重大訊息風險",
}

# ★ 2026-11-08（P0-3 可重現）：特徵 schema 版本戳。每筆候選記錄「當時用哪一版特徵集合」，
# 讓日後重算/訓練能辨識特徵演進、偵測 schema 漂移。特徵集合有增刪時務必 bump 此版本。
FEATURE_SCHEMA_VERSION = "fs-v1-2026-11-08"

_COLS = ("bar_date,ticker,code,name,kind,direction,score,entry,stop,tp1,tp2,tp3,size_cat,sector,"
         "sent,features,status,created_at,model_prob,feat_schema_ver,config_ver,expire_days")


def _current_expire_days():
    from config import CIRCUIT_BREAKER, LEGACY_SIGNAL_EXPIRE_DAYS
    try:
        return int(CIRCUIT_BREAKER.get("signal_expire_days") or LEGACY_SIGNAL_EXPIRE_DAYS)
    except Exception:
        return LEGACY_SIGNAL_EXPIRE_DAYS


def _row_expire_days(row) -> int:
    """這一列適用的到期天數：寫入當下記錄的 expire_days；沒有紀錄者＝2026-11-12 以前的列，適用當時的 15 天。
    規則改為 60 天後，進行中的舊假想單不會在半途被換定義。"""
    from config import LEGACY_SIGNAL_EXPIRE_DAYS
    try:
        v = int((row or {}).get("expire_days") or 0)
    except Exception:
        v = 0
    return v if v > 0 else LEGACY_SIGNAL_EXPIRE_DAYS


def _config_ver():
    """寫入當下的策略版本（config_hash 前 12 碼）；取不到回 None，不猜。"""
    try:
        import strategy_version as _sv
        return _sv.config_hash()[:12]
    except Exception:
        return None

# ───────────── 一次掃描的收集狀態 ─────────────
_run = None
_run_guard = threading.Lock()


def begin_run(note=""):
    """掃描開始時呼叫：之後 signal_engine 才會把被否決的訊號丟進來。"""
    global _run
    with _run_guard:
        _run = {"t0": time.time(), "note": note, "feats": {}, "rejects": [], "controls": [], "lock": threading.Lock(),
                "pool": {}, "matched": {},
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
        # 2026-10-16：新增更細的每筆模擬欄位（舊表以 ALTER TABLE 遷移，PG／SQLite 皆防呆）
        for col in ("r1", "r20", "mae_bars", "mfe_bars", "tp1_before_sl"):
            try:
                store._migrate_add_column("shadow_signals", col, "REAL" if col in ("r1", "r20") else "INTEGER")
            except Exception as e:
                logger.debug(f"shadow migrate {col}: {e}")
        # 2026-11-04（方案 B shadow 平行記錄）：記錄 champion 模型對該筆訊號的預測勝率，
        # 純對帳用、不影響訊號產生。
        try:
            store._migrate_add_column("shadow_signals", "model_prob", "REAL")
        except Exception as e:
            logger.debug(f"shadow migrate model_prob: {e}")
        # 2026-11-06（P0 sent→delivered 對帳）：sent=1 只代表「候選被排入推播」，不等於 Telegram 真的
        # 收下。新增 delivered（provider 至少一個目標回 200）與 delivered_at，讓「送出 vs 送達」分開可查。
        for col, typ in (("delivered", "INTEGER"), ("delivered_at", "TEXT"),
                         ("feat_schema_ver", "TEXT")):
            try:
                store._migrate_add_column("shadow_signals", col, typ)
            except Exception as e:
                logger.debug(f"shadow migrate {col}: {e}")
        # 2026-11-09（Shadow 雙軌）：champion_r＝與正式推播一致的『分批出場（TP1/TP2/TP3 各 1/3、
        # TP1 後移保本、TP2 後移 TP1）』淨 R，回答「現行 Champion 策略到底有沒有優勢」。既有
        # r_multiple 維持單次出場（Research Track），兩軌並存、不混用。
        try:
            store._migrate_add_column("shadow_signals", "champion_r", "REAL")
        except Exception as e:
            logger.debug(f"shadow migrate champion_r: {e}")
        # 2026-11-12（S-P0-01 兩軌生命週期）：champion_r 只存「定案值」（最後一腿真的出場或到期）。
        # champion_status：final＝已定案／provisional＝仍有未出場部位（暫估值另存 champion_r_prov，
        # 不進任何統計）／no_fresh_price＝到期前資料就停更，不以舊收盤價代替／legacy_unverified＝
        # 舊值無法以真實 K 棒重驗。champion_r_legacy＝重算前的舊值（保留供稽核比對，不覆寫、不刪）。
        # champion_exit_date＝Champion 軌最後一腿的出場日（標籤實現日；與 Research 的 exit_date 不同）。
        for col, typ in (("champion_status", "TEXT"), ("champion_r_prov", "REAL"),
                         ("champion_r_legacy", "REAL"), ("champion_exit_date", "TEXT")):
            try:
                store._migrate_add_column("shadow_signals", col, typ)
            except Exception as e:
                logger.debug(f"shadow migrate {col}: {e}")
        # 2026-11-11（策略引擎 Phase 1）：standalone 策略的假想訊號以 strategy_name 標記（如
        # 'trend_following'），既有 Champion 候選視為 'champion'。純 shadow 研究、不影響線上訊號。
        try:
            store._migrate_add_column("shadow_signals", "strategy_name", "TEXT")
        except Exception as e:
            logger.debug(f"shadow migrate strategy_name: {e}")
        # 2026-11-12（S-P0-04 逐列血緣）：config_ver＝這筆被記錄當下的策略版本（config_hash 前 12 碼）；
        # champion_sim_ver＝算出 champion_r 定案值的分批模擬器版本。策略規則一改（例如 11-12 停發放空、
        # 做多門檻 75），同一張表裡就混著不同規則下產生的列；沒有這兩欄就只能靠日期推測。
        # 既有列不回填（當時沒記錄，不用日期反推來假裝有記錄）——NULL＝未記錄。
        for col in ("config_ver", "champion_sim_ver"):
            try:
                store._migrate_add_column("shadow_signals", col, "TEXT")
            except Exception as e:
                logger.debug(f"shadow migrate {col}: {e}")
        # 2026-11-12（出場 Challenger）：alt_exits＝同一筆候選以其他出場方案（execution.EXIT_CHALLENGERS）
        # 模擬的結果（JSON：{方案: {status, r, exit_date}, "sim": 版本}）。只供實驗 E07／E08 比較，
        # 不影響正式訊號，也不進任何既有統計。
        try:
            store._migrate_add_column("shadow_signals", "alt_exits", "TEXT")
        except Exception as e:
            logger.debug(f"shadow migrate alt_exits: {e}")
        # 2026-11-12（到期 15→60 天）：expire_days＝這一列寫入當下適用的到期天數。NULL＝此前的列，適用 15 天。
        try:
            store._migrate_add_column("shadow_signals", "expire_days", "INTEGER")
        except Exception as e:
            logger.debug(f"shadow migrate expire_days: {e}")
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


_ix = {"t": 0, "v": None}


def _index_rets():
    """加權指數近 5/20/60 日報酬(%)，6 小時快取；抓不到回 None（特徵會被省略，不影響掃描）。"""
    try:
        if time.time() - _ix["t"] < 6 * 3600 and _ix["v"] is not None:
            return _ix["v"]
        if time.time() - _ix["t"] < 600:      # 剛失敗過，10 分鐘內不重試
            return _ix["v"]
        _ix["t"] = time.time()
        import yfinance as yf
        h = yf.Ticker("^TWII").history(period="6mo", interval="1d", auto_adjust=False)
        cl = [float(x) for x in h["Close"].dropna()]
        if len(cl) < 62:
            return None
        _ix["v"] = {k: round((cl[-1] / cl[-1 - k] - 1) * 100, 2) for k in (5, 20, 60)}
        _ix["t"] = time.time()
        return _ix["v"]
    except Exception as e:
        logger.debug(f"_index_rets: {e}")
        return _ix["v"]


def tally(stock_info, daily):
    """掃描時對『每一檔』累計產業近 20 日報酬，之後才能算『相對產業強度』（只用候選會偏強，必須用全體）。"""
    r = _run
    if r is None:
        return
    try:
        c = (daily or {}).get("closes") or []
        sec = (stock_info or {}).get("sector")
        if sec and len(c) > 20 and c[-21]:
            with r["lock"]:
                a = r.setdefault("sec", {}).setdefault(sec, [0.0, 0])
                a[0] += (c[-1] / c[-21] - 1) * 100
                a[1] += 1
    except Exception:
        pass


def _add_sector_rel(rows):
    r = _run
    sec = (r or {}).get("sec") or {}
    for x in rows:
        try:
            f = x["features"]
            a = sec.get(x.get("sector"))
            if a and a[1] >= 3 and f.get("ret20") is not None:
                avg = a[0] / a[1]
                f["sec_ret20"] = round(avg, 2)
                f["sec_rel20"] = round(f["ret20"] - avg, 2)
        except Exception:
            pass


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
    # ── 2026-10-06 新增特徵（只記錄、不影響訊號；之後用影子資料驗證哪些真的有預測力）──
    if h and n >= 22:
        f["brk20"] = _pct(px, max(h[-21:-1]))          # 相對「前 20 日最高」：>0 代表已突破前高、<0 代表還在前高之下
        f["pull10"] = _pct(px, max(h[-10:]))           # 距近 10 日高點回檔幅度（越負代表回檔越深）
    if h and n >= 62:
        f["brk60"] = _pct(px, max(h[-61:-1]))
    if n >= 26:
        ma_now, ma_5 = sum(c[-20:]) / 20, sum(c[-25:-5]) / 20
        f["ma20_slope"] = _pct(ma_now, ma_5)           # 20 日均線 5 日斜率(%)：>0 向上
    # ── 2026-10-15 新增搜集特徵（只記錄、不影響訊號；全部由已有的日K陣列直接算，無外部相依）──
    if n >= 6:
        f["dist_ma5"] = _pct(px, sum(c[-5:]) / 5)       # 距 5 日線(%)
        ma5_now, ma5_3 = sum(c[-5:]) / 5, sum(c[-8:-3]) / 5
        f["ma5_slope"] = _pct(ma5_now, ma5_3)           # 5 日線 3 日斜率(%)
    if n >= 2:
        up = 0                                          # 連續上漲（收盤高於前一日）天數；轉負記為負連跌
        for i in range(n - 1, 0, -1):
            if c[i] > c[i - 1]:
                if up < 0: break
                up += 1
            elif c[i] < c[i - 1]:
                if up > 0: break
                up -= 1
            else:
                break
        f["up_days"] = up
    if h and l and px and (h[-1] - l[-1]) > 0:
        f["clv"] = round((c[-1] - l[-1]) / (h[-1] - l[-1]) * 100, 1)  # 收盤在當日高低區間位置(%)：100=收最高
    if len(v) >= 10:
        a1 = sum(v[-5:]) / 5; a0 = sum(v[-10:-5]) / 5
        if a0:
            f["vol_trend5"] = round(a1 / a0, 2)         # 近 5 日均量 ÷ 前 5 日均量：>1 量增
    if n >= 20:
        ma20 = sum(c[-20:]) / 20                        # 連續站上 20 日線天數（趨勢持續性；此處用當前 MA20 近似）
        ab = 0
        for i in range(n - 1, max(-1, n - 61), -1):
            if c[i] >= ma20: ab += 1
            else: break
        f["above20_days"] = ab
    # ── 2026-10-16 擴充搜集（全部純由日K計算、無外部相依；只記錄、不影響訊號分數）──
    if n >= 2:
        f["ret1"] = _pct(px, c[-2])                              # 當日報酬
    if n > 120:
        f["ret120"] = _pct(px, c[-121])                         # 近半年動能
    if n >= 10:
        f["dist_ma10"] = _pct(px, sum(c[-10:]) / 10)
    if n >= 120:
        f["dist_ma120"] = _pct(px, sum(c[-120:]) / 120)
    if h and l and len(h) >= 250 and len(l) >= 250:
        hi52, lo52 = max(h[-250:]), min(l[-250:])
        if hi52 > lo52:
            f["pos52"] = round((px - lo52) / (hi52 - lo52) * 100, 1)   # 52週區間位置：0=最低、100=最高
    if h and l and o and (h[-1] - l[-1]) > 0:                    # K 棒解剖（今日實體／上下影線占比）
        rng = h[-1] - l[-1]
        f["body_pct"] = round(abs(c[-1] - o[-1]) / rng * 100, 1)
        f["uwick_pct"] = round((h[-1] - max(c[-1], o[-1])) / rng * 100, 1)
        f["lwick_pct"] = round((min(c[-1], o[-1]) - l[-1]) / rng * 100, 1)
    if n >= 60:                                                 # 均線多頭排列強度（5>10>20>60 各成立 +1，0~3）
        ma5, ma10, ma20_, ma60 = (sum(c[-k:]) / k for k in (5, 10, 20, 60))
        f["ma_stack"] = int(ma5 > ma10) + int(ma10 > ma20_) + int(ma20_ > ma60)
    if n >= 21:                                                 # 近 20 日最大回檔(%)
        seg = c[-21:]; peak = seg[0]; dd = 0.0
        for p in seg:
            peak = max(peak, p)
            if peak:
                dd = min(dd, (p / peak - 1) * 100)
        f["maxdd20"] = round(dd, 2)
    if n >= 61:                                                 # 60 日年化波動(%)
        rr = [(c[i] / c[i - 1] - 1) for i in range(n - 60, n) if c[i - 1]]
        if len(rr) > 10:
            mu = sum(rr) / len(rr)
            sd = (sum((x - mu) ** 2 for x in rr) / (len(rr) - 1)) ** 0.5
            f["vol60_ann"] = round(sd * (252 ** 0.5) * 100, 1)
    if len(v) >= 21:                                            # 今日量相對近 20 日的 z 分數
        vv = v[-21:-1]; mu = sum(vv) / len(vv)
        sd = (sum((x - mu) ** 2 for x in vv) / (len(vv) - 1)) ** 0.5 if len(vv) > 1 else 0
        if sd:
            f["vol_z20"] = round((v[-1] - mu) / sd, 2)
    if v and px:
        f["turnover_m"] = round(px * v[-1] / 1e6, 1)            # 成交值（百萬元，流動性）
    ix = _index_rets()
    if ix:
        for k in (5, 20, 60):
            if f.get(f"ret{k}") is not None and ix.get(k) is not None:
                f[f"rs{k}"] = round(f[f"ret{k}"] - ix[k], 2)     # 相對加權指數的超額報酬(%)
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
    # ★ 修正：2026-10-09——星期／月份改用「這根 K 棒的日期」而非 now()，否則掃描日≠資料日
    # （隔日補掃、資料落後）時會標錯，污染 learning 的「星期／月份」維度分析。
    bdt = None
    try:
        ds = (daily or {}).get("dates") or []
        if ds:
            bdt = datetime.strptime(str(ds[-1])[:10], "%Y-%m-%d")
    except Exception:
        bdt = None
    bdt = bdt or datetime.now(TPE)
    f["dow"], f["month"] = bdt.weekday(), bdt.month
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
    try:  # 重大訊息：近期公告筆數與是否含負面關鍵字（只記錄）
        from fundamentals import fetch_material_news_risk_map
        items = (fetch_material_news_risk_map() or {}).get(code) or []
        out["news_neg"] = int(bool(items))   # 該 map 只收「命中負面關鍵字」的公司（僅上市）；0 不代表上櫃沒有
    except Exception:
        pass
    if cache is not None:
        cache[code] = out
    return out


# ───────────── 寫入 ─────────────
def _insert(rows):
    """一條連線批次寫入；回傳實際新增筆數（重複的 (日期,股票) 會被略過）。"""
    if not rows:
        return 0
    ensure_table()
    _add_sector_rel(rows)
    from state_store import store
    _cfg_ver = _config_ver()
    _exp_days = _current_expire_days()
    with store._conn() as conn:
        before = (conn.execute("SELECT COUNT(*) AS n FROM shadow_signals").fetchone() or {"n": 0})["n"]
        for r in rows:
            try:
                conn.execute(
                    f"INSERT INTO shadow_signals ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (bar_date, ticker) DO NOTHING",
                    (r["bar_date"], r["ticker"], r["code"], r["name"], r["kind"], r["direction"], r["score"],
                     r["entry"], r["stop"], r["tp1"], r["tp2"], r["tp3"], r["size_cat"], r["sector"],
                     0, json.dumps(r["features"], ensure_ascii=False), "pending", r["created_at"],
                     r.get("model_prob"), FEATURE_SCHEMA_VERSION, _cfg_ver, _exp_days))
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


STOPPED_CLASS_REASONS = ("short_off", "gate_weekly_long", "long_score")   # 2026-11-12 Champion 變更停發的三類
PRIOR_BLOCKED_DAYS = 15                                                    # 回看的日曆天數（＝一筆訊號的有效天數）


def _add_prior_blocked(rows):
    """★ 2026-11-12 特徵 prior_blocked：這檔股票在訊號日之前 15 個日曆天內，是否出現過「被停發類別」的
    假想單（做多但週線不偏多／做多但分數未達 75／放空）。值為最近一筆的原因代碼，沒有則 "none"。
    只記錄、不影響訊號——供實驗 E10 驗證「剛轉強的股票第一時間追進去是否較差」。
    查詢失敗時不寫這個特徵（缺漏＝不猜），不影響候選記錄。"""
    if not rows:
        return
    try:
        from state_store import store
        days = sorted({r["bar_date"] for r in rows})
        lo = (datetime.strptime(days[0], "%Y-%m-%d") - timedelta(days=PRIOR_BLOCKED_DAYS)).strftime("%Y-%m-%d")
        tickers = sorted({r["ticker"] for r in rows})
        prior = {}
        with store._conn() as conn:
            for i in range(0, len(tickers), 250):
                chunk = tickers[i:i + 250]
                ph = ",".join("?" * len(chunk))
                rs = conn.execute(
                    f"SELECT ticker, bar_date, features FROM shadow_signals WHERE kind='rejected' "
                    f"AND bar_date>=? AND bar_date<? AND ticker IN ({ph}) ORDER BY bar_date",
                    (lo, days[-1]) + tuple(chunk)).fetchall()
                for x in rs:
                    try:
                        reason = (json.loads(x["features"] or "{}") or {}).get("reject")
                    except Exception:
                        reason = None
                    if reason in STOPPED_CLASS_REASONS:
                        prior.setdefault(x["ticker"], []).append((x["bar_date"], reason))
        for r in rows:
            floor = (datetime.strptime(r["bar_date"], "%Y-%m-%d") - timedelta(days=PRIOR_BLOCKED_DAYS)).strftime("%Y-%m-%d")
            hits = [(d, why) for d, why in prior.get(r["ticker"], []) if floor <= d < r["bar_date"]]
            r["features"]["prior_blocked"] = hits[-1][1] if hits else "none"
    except Exception as e:
        logger.debug(f"shadow prior_blocked: {e}")


def capture_candidates(signals, market_overview):
    """掃描結束、尚未過濾前呼叫：存下所有候選（含完整特徵）。"""
    t0 = time.time()
    r = _run
    try:
        now = datetime.now(timezone.utc).isoformat()
        cache = r["fund"] if r is not None else {}
        # 方案 B shadow 平行記錄：一次載入 champion 模型，對每筆候選算預測勝率；
        # 任何失敗都不影響候選記錄（沒有模型就留空）。不影響訊號產生。
        _champ = None
        try:
            import learning_model
            _champ = learning_model.load_champion()
        except Exception:
            _champ = None
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
            mp = None
            if _champ is not None:
                try:
                    import learning_model
                    mp = learning_model.prob_with_model(_champ, feats, s.get("score"))
                except Exception:
                    mp = None
            rows.append({"bar_date": bd, "ticker": s["ticker"], "code": s.get("code"), "name": s.get("name"),
                         "kind": "candidate", "direction": s.get("direction"), "score": s.get("score"),
                         "entry": s.get("entry_price"), "stop": s.get("stop_loss"), "tp1": s.get("tp1"),
                         "tp2": s.get("tp2"), "tp3": s.get("tp3"), "size_cat": s.get("size_cat"),
                         "sector": s.get("sector"), "created_at": now, "features": feats, "model_prob": mp})
        _add_prior_blocked(rows)
        if r is not None:
            try:      # S-P1-04：挑配對對照（實際寫入在 flush_controls，與隨機對照一起）
                _tv = {x["ticker"]: (x.get("current_price") or x.get("entry_price") or 0) * (x.get("volume_lots") or 0)
                       for x in (signals or []) if x.get("ticker")}
                _c = [dict(x, turnover=_tv.get(x["ticker"]) or None) for x in rows]
                r["matched"] = _select_matched(_c, r["pool"])
            except Exception as e:
                _err(f"select_matched: {e}")
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
        # ★ 2026-10-09：改為「每個原因隨機抽樣」取代原本「依分數取最高分前 N」。原作法讓被擋下的樣本
        # 系統性偏向最高分的單，與候選基準的分數分布不一致，會放大「這條規則擋掉了賺錢單」的結論偏誤。
        # ★ 2026-11-12：被停發類別（與重挫日）的假想單全數保留、不受上限限制——它們就是「若沒停發會發出的
        # 訊號」，停發前本來是不設上限的候選；實驗 E03～E06 與特徵 prior_blocked 都需要完整紀錄。
        always = []
        for reason, lst in by.items():
            if reason in STOPPED_CLASS_REASONS or reason == "market_stop":
                always += lst
                continue
            random.shuffle(lst)
            keep += lst[:REJ_PER_REASON]
        random.shuffle(keep)
        keep = always + keep[:REJ_TOTAL]
        for x in keep:
            x["features"].update(_fund_features(x["code"], r["fund"]))
        n = _insert(keep)
        logger.info(f"shadow: 被規則擋下的樣本已記錄 {n}/{len(keep)} 筆（本次共遇到 {r['stats']['rej_seen']} 筆，"
                    f"每原因隨機取最多 {REJ_PER_REASON}）")
        r["stats"]["rej_saved"] += n
        return n
    except Exception as e:
        logger.warning(f"shadow.flush_rejects 失敗: {e}")
        _err(f"flush_rejects: {e}")
        return 0
    finally:
        r["stats"]["secs_capture"] += time.time() - t0


def _pool_add(ticker, stock_info, daily, atr, ind=None):
    """把一檔『沒有訊號』的股票記進本次掃描的配對池（精簡欄位，不存整段 K 線）。失敗就略過，不影響掃描。"""
    r = _run
    if r is None:
        return
    try:
        from signal_engine import calc_stop_loss_tw, calc_take_profits_tw
        closes, lows = daily.get("closes") or [], daily.get("lows") or []
        px = closes[-1] if closes else 0
        lots = (stock_info or {}).get("volume_lots")
        bd = ((daily.get("dates") or [""])[-1] or "")[:10]
        if px <= 0 or not atr or not lots or lots <= 0 or not bd:
            return
        size = (stock_info or {}).get("size_cat") or "中型股"
        sl = _as_float(calc_stop_loss_tw("buy", px, atr, ind or {}, size, min(lows[-5:]) if len(lows) >= 5 else None, None))
        if sl is None or sl <= 0 or sl >= px:
            return
        tp = calc_take_profits_tw("buy", px, sl, size)
        code = (stock_info or {}).get("code") or ticker.split(".")[0]
        row = {"bar_date": bd, "ticker": ticker, "code": code, "name": (stock_info or {}).get("name"),
               "kind": "matched", "direction": "buy", "score": None, "entry": px, "stop": round(sl, 2),
               "tp1": tp["tp1"], "tp2": tp["tp2"], "tp3": tp["tp3"], "size_cat": size,
               "sector": (stock_info or {}).get("sector") or "", "turnover": px * lots,
               "created_at": datetime.now(timezone.utc).isoformat()}
        with r["lock"]:
            r["pool"][ticker] = row
    except Exception as e:
        logger.debug(f"shadow pool {ticker}: {e}")


def _select_matched(cands, pool, k=MATCH_K):
    """為每檔做多候選挑 k 檔配對對照：先找同產業且同規模者，依成交金額（對數）最接近排序；
    同產業同規模一檔都沒有時，退到同規模。產業不明（空白／「其他」）視為無產業，直接用同規模。
    回傳 {對照代號: {"row": 池中列, "match_for": [候選代號…], "level": "sector_size"|"size"}}。"""
    import math
    out = {}
    cand_tk = {c["ticker"] for c in cands}
    avail = [p for t, p in pool.items() if t not in cand_tk and p.get("turnover")]
    for c in cands:
        if c.get("direction") != "buy" or not c.get("turnover"):
            continue
        sec, size = (c.get("sector") or ""), c.get("size_cat")
        known = sec and sec != "其他"
        lvl = "sector_size"
        pick = [p for p in avail if known and p["sector"] == sec and p["size_cat"] == size]
        if not pick:
            lvl = "size"
            pick = [p for p in avail if p["size_cat"] == size]
        lt = math.log(c["turnover"])
        for p in sorted(pick, key=lambda p: (abs(math.log(p["turnover"]) - lt), p["ticker"]))[:k]:
            e = out.setdefault(p["ticker"], {"row": p, "match_for": [], "level": lvl})
            e["match_for"].append(c["ticker"])
            if lvl == "sector_size":
                e["level"] = "sector_size"
    return out


def maybe_control(ticker, stock_info, daily, pre, market_overview, sink, lock):
    """被預判跳過的股票，建立「假設做多」的對照單（同一套停損停利機制）。
    ★ 2026-10-09：改用水庫抽樣（與 control_from_engine 一致），取代原本『先到先收前 N 檔』——
    掃描順序固定，先到先收會偏向代號小的股票；水庫抽樣讓每一檔被選進對照組的機率完全相同。"""
    try:
        r = _run
        if r is None:
            return
        try:
            from indicators import calc_atr as _ca
            _a = _ca(daily["highs"], daily["lows"], daily["closes"])
            if _a.get("valid"):
                _pool_add(ticker, stock_info, daily, _a["value"])
        except Exception:
            pass
        with lock:
            r["ctrl_pre_seen"] = r.get("ctrl_pre_seen", 0) + 1
            seen = r["ctrl_pre_seen"]
            if len(sink) < CONTROL_CAP:
                slot = len(sink)
            else:
                j = random.randrange(seen)
                if j >= CONTROL_CAP:
                    return
                slot = j
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
            if slot < len(sink):
                sink[slot] = row
            else:
                sink.append(row)
    except Exception as e:
        logger.debug(f"shadow control {ticker}: {e}")


def control_from_engine(ticker, stock_info, daily, ind, mtf, mkt):
    """signal_engine 判定『這檔沒有訊號』時呼叫：隨機抽 CONTROL_RATE 當對照組（假設做多，同一套停損停利）。
    （日線預篩實際只跳過約 4% 的股票，所以對照組主要從這裡抽，樣本才夠。）"""
    r = _run
    if r is None:
        return
    _pool_add(ticker, stock_info, daily, ((ind or {}).get("atr") or {}).get("value"), ind)
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
        # ★ S-P1-04：配對對照。已在隨機對照裡的股票只加標記（同一檔同一天只能一筆），其餘以 kind='matched' 新增。
        matched = (r or {}).get("matched") or {}
        n_tag = 0
        by_tk = {x["ticker"]: x for x in rows}
        for tk, m in matched.items():
            tag = {"match_for": sorted(set(m["match_for"])), "match_level": m["level"]}
            if tk in by_tk:
                by_tk[tk]["features"].update(tag); n_tag += 1
            else:
                row = {k: v for k, v in m["row"].items() if k != "turnover"}
                row["features"] = dict(tag, turnover_m=round(m["row"]["turnover"] / 1e3, 1))
                rows.append(row)
        for x in rows:
            x["features"].update(_fund_features(x["code"], cache))
        n = _insert(rows)
        logger.info(f"shadow: 對照組已記錄 {n}/{len(rows)} 筆（含配對對照 {len(matched)} 檔，其中 {n_tag} 檔與隨機對照重疊、只加標記）")
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
                # ★ 2026-11-12 對帳口徑修正：排入推播時同步把 delivered 初始化為 0（已確認者保持 1）。
                # delivered 欄 2026-11-06 才新增、且原本只在「送達成功」時寫 1，失敗/未回報與
                # 「欄位還不存在的舊紀錄」同樣是 NULL，無法區分，導致對帳分母把結構上無法對帳的
                # 舊資料也算成「未送達」。初始化為 0 後，往後每筆推播都明確落在可對帳母體內
                # （0＝排入未確認、1＝已確認）。歷史列維持 NULL＝誠實的「無對帳欄位」，不回填。
                conn.execute("UPDATE shadow_signals SET sent=1, delivered=COALESCE(delivered,0) "
                             "WHERE ticker=? AND kind='candidate' AND bar_date="
                             "(SELECT MAX(bar_date) FROM shadow_signals WHERE ticker=? AND kind='candidate')", (t, t))
    except Exception as e:
        logger.warning(f"shadow.mark_sent 失敗: {e}")


def mark_delivered(deliveries):
    """★ 2026-11-06（sent→delivered 對帳）：記錄每檔的 Telegram 實際送達結果。
    deliveries：{ticker: bool}，True＝provider 至少一個目標回 200。只有 True 才設 delivered=1；
    False 保持 delivered=0，使「sent=1 且 delivered=0」＝『排了推播但 TG 沒收下』可被查出。
    對帳用、不影響訊號產生，吞例外不拖垮推播流程。"""
    try:
        if not deliveries:
            return
        ensure_table()
        from state_store import store
        now = datetime.now(timezone.utc).isoformat()
        with store._conn() as conn:
            for t, ok in deliveries.items():
                if not ok:
                    continue
                conn.execute(
                    "UPDATE shadow_signals SET delivered=1, delivered_at=? WHERE ticker=? AND kind='candidate' "
                    "AND bar_date=(SELECT MAX(bar_date) FROM shadow_signals WHERE ticker=? AND kind='candidate')",
                    (now, t, t))
    except Exception as e:
        logger.warning(f"shadow.mark_delivered 失敗: {e}")


# ───────────── 結算 ─────────────
def _simulate(row, bars, expire_days):
    """回傳 dict：result/exit_price/exit_date/bars_held/mfe/mae/gap/r5/r10（皆以 R 為單位）。
    規則與實盤 scanner._resolve_pending_signals 相同：只看訊號日之後的K棒；同日先判停損；
    跳空穿越停損以開盤價成交；逾期（日曆天 ≥ expire_days）以最新收盤價結算為 expired。"""
    planned_entry, stop = row["entry"], row["stop"]
    risk = abs(planned_entry - stop)          # 1R 以「訊號當下規劃的風險」為單位（進場價—停損），跨筆可比
    # ★ 2026-11-06：擋退化風險（entry≈stop）。原本只擋 risk==0，沒擋「極小但非零」，
    # 會讓 (Δ價/risk) 爆量污染 mae/mfe/R。退化列不產 R（回 None，由 resolve_pending 標 bad_risk）。
    if not _risk_ok(planned_entry, stop):
        return None
    after = [b for b in bars if b["bar_date"][:10] > row["bar_date"]]
    if not after:
        return {"result": None, "exit_price": None, "exit_date": None, "bars_held": 0,
                "mfe": None, "mae": None, "gap": None, "r1": None, "r5": None, "r10": None, "r20": None,
                "mae_bars": None, "mfe_bars": None, "tp1_before_sl": None}
    # ★ 2026-11-06 Phase 1：出場判定改用單一權威模擬器 execution.simulate_exit，與（未來的）
    # 回測/paper 共用同一套保守規則，相同輸入必得相同結果。進場價沿用「訊號日後第一根開盤」
    # （與實盤 scanner._resolve_pending_signals 一致），停損/停利維持規劃值，R 分母仍為規劃風險。
    import execution
    entry = after[0].get("open") or planned_entry
    out = execution.simulate_exit(
        planned_entry=planned_entry, entry_fill=entry, stop=stop, direction=row["direction"],
        bars_after=after, bar_date=row["bar_date"], expire_days=expire_days, risk=risk,
        tp1=row.get("tp1"), tp2=row.get("tp2"), tp3=row.get("tp3"),
        prev_close=_signal_close(row, bars))
    return out


def _signal_close(row, bars):
    """訊號日（含）以前最後一根 K 棒的收盤：判定訊號後第一根是否漲跌停鎖死的基準。無則 None。"""
    before = [b for b in bars if b["bar_date"][:10] <= row["bar_date"]]
    return before[-1].get("close") if before else None


def _entry_unfillable(row, bars):
    """★ 2026-11-12（Q-P0-07）：訊號後第一根是否一價鎖死、確定無法進場（做多鎖漲停／放空鎖跌停）。
    這種假想單在現實中買不到／空不到，不得當成已成交的樣本。"""
    after = [b for b in bars if b["bar_date"][:10] > row["bar_date"]]
    if not after:
        return False
    import execution
    return execution.entry_blocked_by_limit(row["direction"], after[0], _signal_close(row, bars))


def _shadow_staged_pnl_factory():
    """回傳一個與 shadow 單次出場『同口徑』的 pnl_fn：滑價＋來回手續費＋證交稅＋（放空）借券費，
    唯張數相消、不套用每筆 20 元最低手續費（與既有 r_multiple 相同假設，兩軌才可直接比較）。"""
    from config import COMMISSION_RATE, TAX_RATE_SELL, SLIPPAGE_RATE, SHORT_BORROW_ANNUAL

    def _pnl(entry, exit_price, direction, shares, days_held=0):
        buy = direction == "buy"
        eff_in = entry * (1 + SLIPPAGE_RATE) if buy else entry * (1 - SLIPPAGE_RATE)
        eff_out = exit_price * (1 - SLIPPAGE_RATE) if buy else exit_price * (1 + SLIPPAGE_RATE)
        gross = (eff_out - eff_in) * shares if buy else (eff_in - eff_out) * shares
        sell_leg = eff_out if buy else eff_in                       # 賣出腳的每股價（證交稅／借券基準）
        cost = (eff_in * COMMISSION_RATE + eff_out * COMMISSION_RATE + sell_leg * TAX_RATE_SELL) * shares
        borrow = sell_leg * shares * SHORT_BORROW_ANNUAL * max(0, days_held) / 365 if not buy else 0
        return gross - cost - borrow
    return _pnl


def _champion_r(row, bars, expire_days):
    """Champion Track：以『分批出場』（與正式推播一致）＋ shadow 成本口徑計淨 R。
    回傳 champion_r（分批淨損益 ÷ (1R×股數)，股數相消故與名目股數無關）或 None（資料不足/退化）。"""
    planned_entry, stop = row.get("entry"), row.get("stop")
    if not _risk_ok(planned_entry, stop):
        return None
    after = [b for b in bars if b["bar_date"][:10] > row["bar_date"]]
    if not after:
        return None
    import execution
    risk = abs(planned_entry - stop)
    fill = after[0].get("open") or planned_entry                   # 與單次出場同基準（訊號日後第一根開盤）
    shares = 3000                                                  # 名目股數（可被 3 整除，R 正規化後相消）
    sim = execution.simulate_staged_exit(
        after, entry=fill, sl=stop, tp1=row.get("tp1"), tp2=row.get("tp2"), tp3=row.get("tp3"),
        direction=row["direction"], shares=shares, gen_date=row["bar_date"],
        pnl_fn=_shadow_staged_pnl_factory(), expire_days=expire_days)
    if not sim:
        return None
    return round(sim["pnl"] / (risk * shares), 3)


CHAMPION_GRACE_DAYS = 7      # 到期後仍等不到「到期日當天或之後」的 K 棒，超過此寬限即判 no_fresh_price


def _champion_track(row, bars, expire_days, today=None):
    """★ 2026-11-12（S-P0-01）Champion 軌生命週期：回傳 {"r": 淨R, "status": ...} 或 None（無資料／退化）。

    Research 軌（單次出場）在第一次觸價就結案；但 Champion 軌是分批出場，TP1 觸及後還有 2/3
    部位在場。原本在 Research 結案當下就把 champion_r 寫死，而 simulate_staged_exit 對「還沒出完、
    也還沒到期」的部位是用『最後一根已知收盤』暫估——之後不再更新，暫估值就被當成定案值。

    status：
      final          最後一腿已真的出場（停損／TP3），或已有「到期日當天或之後」的 K 棒可結算。
      provisional    仍有未出場部位且尚未到期：r 為以最新收盤暫估，僅供參考，不得進統計。
      no_fresh_price 已過到期日＋寬限，但 K 棒在到期日之前就停更：不以舊收盤價當到期價，r=None。
    """
    planned_entry, stop = row.get("entry"), row.get("stop")
    if not _risk_ok(planned_entry, stop):
        return None
    after = [b for b in bars if b["bar_date"][:10] > row["bar_date"]]
    if not after:
        return None
    import execution
    risk = abs(planned_entry - stop)
    fill = after[0].get("open") or planned_entry                   # 與單次出場同基準（訊號日後第一根開盤）
    shares = 3000                                                  # 名目股數（可被 3 整除，R 正規化後相消）
    sim = execution.simulate_staged_exit(
        after, entry=fill, sl=stop, tp1=row.get("tp1"), tp2=row.get("tp2"), tp3=row.get("tp3"),
        direction=row["direction"], shares=shares, gen_date=row["bar_date"],
        pnl_fn=_shadow_staged_pnl_factory(), expire_days=expire_days,
        prev_close=_signal_close(row, bars))
    if not sim:
        return None
    r = round(sim["pnl"] / (risk * shares), 3)
    if not execution.staged_open_at_end(sim, row["bar_date"], expire_days):
        return {"r": r, "status": "final", "exit_date": sim.get("exit_date")}
    try:
        ref = today or datetime.now(TPE).replace(tzinfo=None)
        age = (ref - datetime.strptime(row["bar_date"][:10], "%Y-%m-%d")).days
    except Exception:
        age = 0
    if expire_days and age >= expire_days + CHAMPION_GRACE_DAYS:
        return {"r": None, "status": "no_fresh_price"}
    return {"r": r, "status": "provisional"}


def _champion_fields(track):
    """把 _champion_track 的結果轉成 UPDATE 欄位：champion_r 只在 final 時寫入。"""
    if not track:
        return [], []
    st = track["status"]
    if st == "final":
        import execution
        return (["champion_r=?", "champion_r_prov=NULL", "champion_status='final'", "champion_exit_date=?",
                 "champion_sim_ver=?"],
                [track["r"], track.get("exit_date"), execution.STAGED_SIM_VERSION])
    if st == "provisional":
        return ["champion_r=NULL", "champion_r_prov=?", "champion_status='provisional'"], [track["r"]]
    return ["champion_r=NULL", "champion_r_prov=NULL", "champion_status='no_fresh_price'"], []


def _load_bars(tickers, since):
    """一條連線、分批 IN 查詢，撈回所有需要的日K（取代「一檔一條連線」）。"""
    from state_store import store
    import execution
    out = {}
    with store._conn() as conn:
        for i in range(0, len(tickers), 250):
            chunk = tickers[i:i + 250]
            ph = ",".join("?" * len(chunk))
            rs = conn.execute(
                f"SELECT ticker,bar_date,open,high,low,close FROM ohlcv_bars WHERE tf_key='daily' "
                f"AND ticker IN ({ph}) AND bar_date>=? ORDER BY ticker,bar_date", tuple(chunk) + (since,)).fetchall()
            for x in rs:
                b = dict(x)
                # 無效（NaN／缺值／非正數）K 棒不進模擬：否則出場條件永不成立、哨兵值會寫進資料庫
                if execution.bar_ok(b) and b.get("open") == b.get("open"):
                    out.setdefault(x["ticker"], []).append(b)
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


_CLEANUP_META = "shadow_absurd_cleanup_v1"
_cleanup_done = False


def cleanup_absurd_metrics(force: bool = False) -> dict:
    """★ 2026-11-06（P0 資料可信度）：一次性清洗既有爆量髒列。
    把任一 R 欄位 |值| > _ABSURD_R（非物理值）的列：R 欄位全部清 NULL、狀態改
    unresolved/result=bad_risk，使其離開勝率與平均 R 統計、也不再被 learning 當訓練標籤。
    以 state_store meta 旗標保證整個資料庫只跑一次（force=True 可手動重跑）。回傳處理筆數。"""
    # ★ 2026-11-12：原本以 meta 旗標保證「整個資料庫只跑一次」。但上線後的稽核計數顯示清洗過後
    # 仍有 1 筆 |MFE/MAE|>1000 的列——髒列會再生（見 resolve_pending 選列條件的修正），一次性清洗
    # 抓不到之後才出現的。改為每次結算都檢查（一次 COUNT，成本很低）；清洗前先把問題列的
    # 識別資訊寫進日誌，才查得出是哪一檔、為什麼。force 參數保留相容。
    global _cleanup_done
    try:
        from state_store import store
        ensure_table()
        cond = " OR ".join(f"ABS({c}) > ?" for c in _R_COLS)
        params = tuple([_ABSURD_R] * len(_R_COLS))
        null_set = ", ".join(f"{c}=NULL" for c in _R_COLS)
        with store._conn() as conn:
            n = conn.execute(f"SELECT COUNT(*) AS n FROM shadow_signals WHERE {cond}", params).fetchone()
            n = (dict(n).get("n") if n else 0) or 0
            if n:
                try:
                    bad = [dict(x) for x in conn.execute(
                        f"SELECT id,ticker,bar_date,kind,direction,status,result,entry,stop,r_multiple,mae_r,mfe_r "
                        f"FROM shadow_signals WHERE {cond} LIMIT 20", params).fetchall()]
                    logger.warning(f"shadow.cleanup_absurd_metrics：發現 {n} 筆非物理值列（清洗前明細，最多 20 筆）：{bad}")
                except Exception as _e:
                    logger.debug(f"cleanup_absurd_metrics detail: {_e}")
                conn.execute(
                    f"UPDATE shadow_signals SET {null_set}, status='unresolved', result='bad_risk' "
                    f"WHERE {cond}", params)
        if n or not _cleanup_done:
            store.set_meta(_CLEANUP_META, {"at": datetime.now(timezone.utc).isoformat(), "cleaned": int(n)})
        _cleanup_done = True
        if n:
            logger.info(f"shadow.cleanup_absurd_metrics：清洗 {n} 筆爆量髒列（|R|>{_ABSURD_R:.0f}），已標 bad_risk")
        return {"ran": True, "cleaned": int(n)}
    except Exception as e:
        logger.warning(f"shadow.cleanup_absurd_metrics 失敗（不影響結算）: {e}")
        return {"ran": False, "error": str(e)}


def resolve_pending(max_rows=8000):
    """掃描完成後呼叫：用資料庫已更新的日K快取結算（不對外連線）。
    只寫入『有變動』的列，並把 r5/r10 補齊給近 30 天的列。"""
    t0 = time.time()
    r = _run
    res = {}
    try:
        ensure_table()
        from state_store import store
        from config import CIRCUIT_BREAKER, COMMISSION_RATE, TAX_RATE_SELL, SLIPPAGE_RATE, SHORT_BORROW_ANNUAL
        cleanup_absurd_metrics()      # 每次結算都檢查非物理值列（2026-11-12 起不再只跑一次）
        cutoff = (datetime.now(TPE) - timedelta(days=45)).strftime("%Y-%m-%d")
        # 退市／長期無資料的訊號若一直留在 pending，壞結果（下市、停牌）會從統計中消失（存活者偏誤）。
        # 超過 expire_days + 寬限天數仍抓不到任何 K 棒，標記為 unresolved（不計入勝率與平均 R），
        # 離開 pending 佇列並在報告揭露數量，讓「有多少訊號無法驗證」被看見而非被無聲略過。
        NODATA_GRACE = 7
        _today = datetime.now(TPE).replace(tzinfo=None)
        unresolved = []
        bad_risk = []       # ★ 2026-11-06：entry≈stop 退化列，R 無意義，標 unresolved/bad_risk 離開統計
        no_new_bars = []    # ★ 2026-11-12：有舊 K 棒但訊號日後無新棒（停牌/下市/停更），逾期後離開佇列
        no_fresh_price = [] # ★ 2026-11-12：K 棒在到期日前就停更，不以舊收盤價當到期價（S-P1-02）
        unfilled_limit = [] # ★ 2026-11-12：進場當根漲跌停鎖死、確定無法成交（Q-P0-07）
        with store._conn() as conn:
            rows = [dict(x) for x in conn.execute(
                # ★ 2026-11-12：補 r5/r10 只針對「已結案」的列。原本條件是 (r10 IS NULL AND 近期)，
                # 連已標 unresolved 的列也會每次被撈回來重新模擬、重寫 MFE/MAE——被清洗掉的髒值因此
                # 再生（線上日誌：每次結算都重複出現 bad_risk 2 筆、更新 3 筆），也白做工。
                # ★ 2026-11-12（S-P1-04）：改為補到 r20 為止（原本只補到 r10）。先停損出場的列原本永遠拿不到
                # 20 日報酬，配對比較只剩沒停損的列 → 系統性偏樂觀。回看由 30 天放寬為 45 天（20 根 K 棒約 28 天）。
                "SELECT * FROM shadow_signals WHERE status='pending' "
                "OR (status='closed' AND r20 IS NULL AND bar_date>=?) "
                "ORDER BY bar_date LIMIT ?", (cutoff, max_rows)).fetchall()]
        if not rows:
            logger.info("shadow: 結算完成，沒有需要處理的紀錄")
            return {"updated": 0, "closed": 0, "rows": 0}
        if len(rows) >= max_rows:
            # 到期延長為 60 天後，未觸價的假想單會在佇列裡留更久；撈滿上限代表較新的列這次沒輪到（下次再結算）。
            logger.warning(f"shadow: 待結算列數達單次上限 {max_rows}，較新的列順延到下次結算")
        since = (datetime.strptime(min(x["bar_date"] for x in rows), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
        bars_by = _load_bars(sorted({x["ticker"] for x in rows}), since)
        updates, closed, rescaled = [], 0, 0
        for row in rows:
            expire_days = _row_expire_days(row)      # 逐列：舊列 15 天、2026-11-12 之後寫入者依當時設定
            try:
                age = (_today - datetime.strptime(row["bar_date"], "%Y-%m-%d")).days
            except Exception:
                age = 0
            bars = bars_by.get(row["ticker"])
            if not bars:
                # 無任何 K 棒：超過到期＋寬限仍抓不到就標記 unresolved，否則留著下次再試
                if row["status"] == "pending" and age >= expire_days + NODATA_GRACE:
                    unresolved.append(row["id"])
                continue
            # ★ 2026-11-12 永久卡單修正：標的有「舊」K 棒、但訊號日之後一根都沒有（停牌／下市／
            # 資料源停更）。此時 _simulate 必回 result=None，原本不會被上面的 `not bars` 逾期保護
            # 攔到，於是永遠留在 pending——後台因此長期顯示「超過有效期，可能結算流程卡住」。
            # 逾期＋寬限後標 unresolved/no_new_bars 離開佇列，於原因拆分誠實列出；
            # 不猜測結果、不計入勝率與平均 R（與 nodata 同樣的誠實處理）。
            if (row["status"] == "pending" and age >= expire_days + NODATA_GRACE
                    and not any(b["bar_date"][:10] > row["bar_date"] for b in bars)):
                no_new_bars.append(row["id"])
                continue
            r2, scale = _rescale(row, bars)
            rescaled += scale != 1.0
            if not _risk_ok(r2.get("entry"), r2.get("stop")):
                bad_risk.append(row["id"])          # 退化風險：不結算、離開佇列、不入統計
                continue
            # ★ 2026-11-12（Q-P0-07）：訊號後第一根一價鎖死（做多鎖漲停／放空鎖跌停）＝確定無法進場，
            # 標 unresolved/unfilled_limit 離開佇列，不當成已成交樣本、不計入勝率與平均 R。
            if row["status"] == "pending" and _entry_unfillable(r2, bars):
                unfilled_limit.append(row["id"])
                continue
            sim = _simulate(r2, bars, expire_days)
            if not sim:
                continue
            # ★ 2026-11-12（S-P1-02）：到期結算必須有「到期日當天或之後」的 K 棒。simulate_exit 在
            # 沒有這種 K 棒時會以『最後一根已知收盤』結算 expired——若資料在到期日前就停更，那是
            # 一個過期價。此時先留 pending 等新棒；超過寬限仍等不到 → unresolved/no_fresh_price，
            # 不以舊收盤價當到期價、不計入勝率與平均 R。
            if row["status"] == "pending" and sim.get("result") == "expired" and sim.get("exit_date"):
                try:
                    _exit_age = (datetime.strptime(sim["exit_date"], "%Y-%m-%d")
                                 - datetime.strptime(row["bar_date"], "%Y-%m-%d")).days
                except Exception:
                    _exit_age = expire_days
                if _exit_age < expire_days:
                    if age >= expire_days + NODATA_GRACE:
                        no_fresh_price.append(row["id"])
                        continue
                    sim = dict(sim, result=None, exit_price=None, exit_date=None)
            new = {"gap_pct": sim["gap"], "r1": sim["r1"], "r5": sim["r5"], "r10": sim["r10"], "r20": sim["r20"],
                   "mfe_r": sim["mfe"], "mae_r": sim["mae"], "bars_held": sim["bars_held"],
                   "mae_bars": sim["mae_bars"], "mfe_bars": sim["mfe_bars"], "tp1_before_sl": sim["tp1_before_sl"]}
            newly_closed = row["status"] == "pending" and sim["result"]
            changed = newly_closed or any(not _same(row.get(k), v) for k, v in new.items())
            if not changed:
                continue
            fields, vals = [f"{k}=?" for k in new], list(new.values())
            if newly_closed:
                risk = abs(r2["entry"] - r2["stop"]); buy = row["direction"] == "buy"
                fill = sim.get("entry_fill") or r2["entry"]      # 以實際開盤進場價計 R，與 _simulate 的 mfe/mae/r5/r10 同基準
                exitp = sim["exit_price"]
                # ★ 2026-10-09：以「真實交易」為目標，淨 R 納入滑價、來回手續費＋證交稅、放空借券費，
                # 與實盤 calc_tw_pnl 同一套口徑（唯張數相消、不套用每筆 20 元最低手續費）。
                eff_fill = fill * (1 + SLIPPAGE_RATE) if buy else fill * (1 - SLIPPAGE_RATE)
                eff_exit = exitp * (1 - SLIPPAGE_RATE) if buy else exitp * (1 + SLIPPAGE_RATE)
                gross_r = (eff_exit - eff_fill) / risk if buy else (eff_fill - eff_exit) / risk
                sell_leg = eff_exit if buy else eff_fill
                cost_ps = eff_fill * COMMISSION_RATE + eff_exit * COMMISSION_RATE + sell_leg * TAX_RATE_SELL
                try:
                    days_held = max(0, (datetime.strptime(sim["exit_date"], "%Y-%m-%d")
                                        - datetime.strptime(row["bar_date"], "%Y-%m-%d")).days)
                except Exception:
                    days_held = 0
                borrow_ps = sell_leg * SHORT_BORROW_ANNUAL * days_held / 365 if not buy else 0
                net_r = gross_r - (cost_ps + borrow_ps) / risk
                fields += ["status='closed'", "result=?", "exit_price=?", "exit_date=?", "r_multiple=?"]
                vals += [sim["result"], exitp, sim["exit_date"], round(net_r, 3)]
                # ★ 2026-11-09 Shadow 雙軌：另存 Champion Track（分批出場）淨 R，與單次出場 r_multiple 並列。
                # ★ 2026-11-12（S-P0-01）：Research 結案不等於 Champion 結案。仍有未出場部位者只記
                # 暫估（champion_r_prov / provisional），由 finalize_champion_tracks 於最後一腿
                # 真的出場或到期後才寫入 champion_r。
                try:
                    _cf, _cv = _champion_fields(_champion_track(r2, bars, expire_days, today=_today))
                except Exception as e:
                    _cf, _cv = [], []
                    logger.debug(f"champion_r 計算失敗 id={row['id']}: {e}")
                fields += _cf; vals += _cv
                closed += 1
            updates.append((f"UPDATE shadow_signals SET {', '.join(fields)} WHERE id=?", vals + [row["id"]]))
        if updates:
            with store._conn() as conn:
                for q, v in updates:
                    conn.execute(q, v)
        if unresolved:
            with store._conn() as conn:
                for _id in unresolved:
                    conn.execute("UPDATE shadow_signals SET status='unresolved', result='nodata' WHERE id=?", (_id,))
            logger.info(f"shadow: {len(unresolved)} 筆訊號逾期仍無 K 棒（疑似退市／停牌），標記 unresolved")
        if bad_risk:
            with store._conn() as conn:
                for _id in bad_risk:
                    conn.execute("UPDATE shadow_signals SET status='unresolved', result='bad_risk' WHERE id=?", (_id,))
            logger.info(f"shadow: {len(bad_risk)} 筆訊號進場價≈停損（風險退化、R 無意義），標記 unresolved/bad_risk")
        if no_new_bars:
            with store._conn() as conn:
                for _id in no_new_bars:
                    conn.execute("UPDATE shadow_signals SET status='unresolved', result='no_new_bars' WHERE id=?", (_id,))
            logger.info(f"shadow: {len(no_new_bars)} 筆訊號逾期仍無『訊號日之後』的 K 棒"
                        f"（疑似停牌／下市／資料源停更），標記 unresolved/no_new_bars")
        if no_fresh_price:
            with store._conn() as conn:
                for _id in no_fresh_price:
                    conn.execute("UPDATE shadow_signals SET status='unresolved', result='no_fresh_price' WHERE id=?", (_id,))
            logger.info(f"shadow: {len(no_fresh_price)} 筆訊號逾期但 K 棒在到期日前就停更，"
                        f"不以舊收盤價結算，標記 unresolved/no_fresh_price")
        if unfilled_limit:
            with store._conn() as conn:
                for _id in unfilled_limit:
                    conn.execute("UPDATE shadow_signals SET status='unresolved', result='unfilled_limit' WHERE id=?", (_id,))
            logger.info(f"shadow: {len(unfilled_limit)} 筆訊號進場當根漲跌停鎖死、無法成交，標記 unresolved/unfilled_limit")
        res = {"updated": len(updates), "closed": closed, "rows": len(rows),
               "rescaled": int(rescaled), "unresolved": len(unresolved), "bad_risk": len(bad_risk),
               "no_new_bars": len(no_new_bars), "no_fresh_price": len(no_fresh_price),
               "unfilled_limit": len(unfilled_limit)}
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


def backfill_champion_r(max_rows=20000) -> dict:
    """一次性回填 champion_r（Shadow 雙軌 #140 上線前就結案的候選列，champion_r 為 NULL）。

    ★ 只用真實資料：訊號當時記錄的 entry/stop/tp（真值）＋ ohlcv_bars 的真實歷史 K 棒，跑
    execution.simulate_staged_exit 重算，與新結案時 resolve_pending 的 _champion_r 完全同一套。
    缺真實 K 棒或風險退化者 **留 NULL、不猜、不填代用值**；不改動 r_multiple/result/exit 等既有欄位。
    冪等：只填 champion_r IS NULL 的列，可重複執行。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM shadow_signals WHERE status='closed' AND kind='candidate' "
            "AND champion_r IS NULL AND r_multiple IS NOT NULL AND result != 'bad_risk' "
            "ORDER BY bar_date LIMIT ?", (max_rows,)).fetchall()]
    if not rows:
        return {"scanned": 0, "filled": 0, "skipped_nodata": 0}
    since = (datetime.strptime(min(r["bar_date"] for r in rows), "%Y-%m-%d")
             - timedelta(days=10)).strftime("%Y-%m-%d")
    bars_by = _load_bars(sorted({r["ticker"] for r in rows}), since)
    updates, skipped = [], 0
    for row in rows:
        bars = bars_by.get(row["ticker"])
        if not bars or not _risk_ok(row.get("entry"), row.get("stop")):
            skipped += 1
            continue
        r2, _scale = _rescale(row, bars)          # 與 resolve_pending 相同的除權息還原口徑
        try:
            track = _champion_track(r2, bars, _row_expire_days(row))
        except Exception as e:
            logger.debug(f"backfill champion_r id={row['id']}: {e}")
            track = None
        if track is None or track["status"] != "final":   # 缺真實 K 棒/退化/尚未定案 → 留 NULL，不猜
            skipped += 1
            continue
        updates.append((track["r"], track.get("exit_date"), row["id"]))
    if updates:
        import execution as _exec
        with store._conn() as conn:
            for champ, _xd, _id in updates:
                conn.execute("UPDATE shadow_signals SET champion_r=?, champion_status='final', "
                             "champion_exit_date=?, champion_sim_ver=? WHERE id=?",
                             (champ, _xd, _exec.STAGED_SIM_VERSION, _id))
    logger.info(f"champion_r 回填：掃 {len(rows)} 筆、填 {len(updates)} 筆、缺真實資料略過 {skipped} 筆")
    return {"scanned": len(rows), "filled": len(updates), "skipped_nodata": skipped}


def _alt_exit_tracks(row, bars, today=None):
    """★ 2026-11-12 出場 Challenger：以 execution.EXIT_CHALLENGERS 的各方案模擬同一筆假想單。
    回傳 {方案: {"status": final／provisional／no_fresh_price／no_atr, "r": 淨R或None, "exit_date"}}；
    無資料／退化風險回 None。進場、成本、R 的分母與 Champion 軌完全相同，只有出場規則不同：
      final          部位已全部出場（停損／停利／移動停損／到期）。
      provisional    還有部位在場、也還沒到期：不存數字（不以暫估值當結果）。
      no_fresh_price 已過到期日＋寬限，K 棒卻在到期前就停更。
      no_atr         方案需要 ATR，但這筆沒有記錄停損的 ATR 倍數：不猜。"""
    planned_entry, stop = row.get("entry"), row.get("stop")
    if not _risk_ok(planned_entry, stop):
        return None
    after = [b for b in bars if b["bar_date"][:10] > row["bar_date"]]
    if not after:
        return None
    import execution
    risk = abs(planned_entry - stop)
    fill = after[0].get("open") or planned_entry
    shares = 3000
    try:
        f = row.get("features")
        f = json.loads(f) if isinstance(f, str) else (f or {})
        sl_atr = float(f.get("sl_atr")) if f.get("sl_atr") else None
    except Exception:
        sl_atr = None
    atr = risk / sl_atr if sl_atr and sl_atr > 0 else None         # 與停損同一把尺（還原縮放後仍一致）
    try:
        ref = today or datetime.now(TPE).replace(tzinfo=None)
        age = (ref - datetime.strptime(row["bar_date"][:10], "%Y-%m-%d")).days
    except Exception:
        age = 0
    out = {}
    own_expire = _row_expire_days(row)
    for name, spec in execution.EXIT_CHALLENGERS.items():
        if spec.get("ladder", True) and not spec.get("trail_atr") and spec.get("expire") == own_expire:
            continue                    # 與這一列自己的 Champion 出場完全相同：不重複計算（結果就是 champion_r）
        if spec.get("trail_atr") and not atr:
            out[name] = {"status": "no_atr", "r": None}
            continue
        sim = execution.simulate_alt_exit(spec, after, fill, stop, row.get("tp1"), row.get("tp2"), row.get("tp3"),
                                          row["direction"], shares, row["bar_date"], _shadow_staged_pnl_factory(),
                                          atr=atr, prev_close=_signal_close(row, bars))
        if sim is None:
            out[name] = {"status": "no_atr" if spec.get("trail_atr") else "provisional", "r": None}
        elif not sim["open_end"]:
            out[name] = {"status": "final", "r": round(sim["pnl"] / (risk * shares), 3), "exit_date": sim["exit_date"]}
        elif age >= spec["expire"] + CHAMPION_GRACE_DAYS:
            out[name] = {"status": "no_fresh_price", "r": None}
        else:
            out[name] = {"status": "provisional", "r": None}
    return out


def finalize_alt_exit_tracks(max_rows=5000, today=None) -> dict:
    """出場 Challenger 的定案：對「Champion 軌已定案」的候選，補算／更新其他出場方案的結果。
    只讀資料庫既有日 K、冪等、可重複執行；全部方案都定案（或確定無法計算）後就不再重算。"""
    ensure_table()
    from state_store import store
    import execution
    ref = today or datetime.now(TPE).replace(tzinfo=None)
    cutoff = (ref - timedelta(days=150)).strftime("%Y-%m-%d")
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM shadow_signals WHERE kind='candidate' AND status='closed' AND champion_status='final' "
            "AND bar_date>=? AND (alt_exits IS NULL OR alt_exits LIKE ?) ORDER BY bar_date LIMIT ?",
            (cutoff, '%"provisional"%', max_rows)).fetchall()]
    out = {"scanned": len(rows), "updated": 0, "no_bars": 0}
    out.update({f"{k}_final": 0 for k in execution.EXIT_CHALLENGERS})
    if not rows:
        return out
    since = (datetime.strptime(min(r["bar_date"] for r in rows), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    bars_by = _load_bars(sorted({r["ticker"] for r in rows}), since)
    updates = []
    for row in rows:
        bars = bars_by.get(row["ticker"])
        if not bars:
            out["no_bars"] += 1
            continue
        try:
            r2, _scale = _rescale(row, bars)
            tracks = _alt_exit_tracks(r2, bars, today=ref)
        except Exception as e:
            logger.debug(f"alt exit id={row['id']}: {e}")
            tracks = None
        if not tracks:
            continue
        for k, t in tracks.items():
            out[f"{k}_final"] += t["status"] == "final"
        tracks["sim"] = execution.ALT_EXIT_SIM_VERSION
        new = json.dumps(tracks, ensure_ascii=False, sort_keys=True)
        if new != row.get("alt_exits"):
            updates.append((new, row["id"]))
    if updates:
        with store._conn() as conn:
            for v, _id in updates:
                conn.execute("UPDATE shadow_signals SET alt_exits=? WHERE id=?", (v, _id))
    out["updated"] = len(updates)
    logger.info(f"shadow 出場 Challenger 定案：{out}")
    return out


def finalize_champion_tracks(max_rows=5000, today=None) -> dict:
    """★ 2026-11-12（S-P0-01）Champion 軌定案與舊值重驗。只讀資料庫既有日 K、冪等、可重複執行。

    處理兩種列（皆為 Research 已結案者）：
      1) champion_status 為 provisional／no_fresh_price：有新 K 棒後重算，最後一腿真的出場或到期
         才寫入 champion_r（final）。
      2) 舊列（champion_status 為 NULL 且已有 champion_r）：這些值是在 Research 結案當下寫死的，
         可能含暫估。以真實 K 棒重算；舊值一律先存進 champion_r_legacy（不覆寫、不刪除），再依
         重算結果寫入 final／provisional／no_fresh_price；K 棒已不存在而無法重驗者標
         legacy_unverified、移出統計（值仍留在 champion_r_legacy）。
    回傳含新舊差異統計，供稽核（不是只宣稱已修）。"""
    ensure_table()
    from state_store import store
    ref = today or datetime.now(TPE).replace(tzinfo=None)
    cutoff = (ref - timedelta(days=150)).strftime("%Y-%m-%d")     # 到期最長 60 天＋寬限，留足回看
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM shadow_signals WHERE status='closed' AND r_multiple IS NOT NULL AND ("
            "(champion_status IN ('provisional','no_fresh_price') AND bar_date>=?) "
            "OR (champion_status IS NULL AND champion_r IS NOT NULL)) "
            "ORDER BY bar_date LIMIT ?", (cutoff, max_rows)).fetchall()]
    out = {"scanned": len(rows), "finalized": 0, "still_provisional": 0, "no_fresh_price": 0,
           "legacy_checked": 0, "legacy_changed": 0, "legacy_unverified": 0,
           "legacy_mean_abs_diff": None, "legacy_max_abs_diff": None}
    if not rows:
        return out
    since = (datetime.strptime(min(r["bar_date"] for r in rows), "%Y-%m-%d")
             - timedelta(days=10)).strftime("%Y-%m-%d")
    bars_by = _load_bars(sorted({r["ticker"] for r in rows}), since)
    updates, diffs = [], []
    for row in rows:
        legacy = row.get("champion_status") is None
        bars = bars_by.get(row["ticker"])
        track = None
        if bars:
            try:
                r2, _scale = _rescale(row, bars)
                track = _champion_track(r2, bars, _row_expire_days(row), today=ref)
            except Exception as e:
                logger.debug(f"finalize champion id={row['id']}: {e}")
        if legacy:
            out["legacy_checked"] += 1
            old = row.get("champion_r")
            if track is None:
                out["legacy_unverified"] += 1
                updates.append(("UPDATE shadow_signals SET champion_r_legacy=COALESCE(champion_r_legacy,?), "
                                "champion_r=NULL, champion_status='legacy_unverified' WHERE id=?",
                                [old, row["id"]]))
                continue
            f, v = _champion_fields(track)
            updates.append((f"UPDATE shadow_signals SET champion_r_legacy=COALESCE(champion_r_legacy,?), "
                            f"{', '.join(f)} WHERE id=?", [old] + v + [row["id"]]))
            if track["status"] == "final" and old is not None:
                d = abs(track["r"] - old)
                diffs.append(d)
                out["legacy_changed"] += d > 0.0005
        else:
            if track is None:
                continue
            if track["status"] == row.get("champion_status") and track["status"] != "provisional":
                continue
            f, v = _champion_fields(track)
            updates.append((f"UPDATE shadow_signals SET {', '.join(f)} WHERE id=?", v + [row["id"]]))
        if track is not None:
            key = {"final": "finalized", "provisional": "still_provisional",
                   "no_fresh_price": "no_fresh_price"}[track["status"]]
            out[key] += 1
    if updates:
        with store._conn() as conn:
            for q, v in updates:
                conn.execute(q, v)
    if diffs:
        out["legacy_mean_abs_diff"] = round(sum(diffs) / len(diffs), 4)
        out["legacy_max_abs_diff"] = round(max(diffs), 3)
    logger.info(f"shadow Champion 軌定案：{out}")
    return out


def _safe_cohorts():
    try:
        return cohort_report()
    except Exception as e:
        logger.debug(f"cohort_report: {e}")
        return None


def data_quality_counts() -> dict:
    """★ 2026-11-12（S-P0-02）：影子資料的不變量稽核計數（只讀）。正式資料庫不對外開放，
    這些數字寫進結算工作的日誌，才能確認歷史髒資料（缺停損的對照組、±9e9 的 MFE/MAE 哨兵值）
    是否真的已清乾淨——不是只憑「清洗程式有測試」就宣稱已修。"""
    ensure_table()
    from state_store import store
    q = {
        "closed_without_r": "status='closed' AND r_multiple IS NULL",
        "closed_missing_stop": "status='closed' AND (stop IS NULL OR stop<=0 OR entry IS NULL OR entry<=0)",
        "absurd_mfe_mae": "(ABS(mfe_r)>1000 OR ABS(mae_r)>1000)",
        "absurd_r": "ABS(r_multiple)>50",
        "champion_provisional": "champion_status='provisional'",
        "champion_no_fresh_price": "champion_status='no_fresh_price'",
        "champion_legacy_unverified": "champion_status='legacy_unverified'",
        "champion_legacy_pending_reaudit": "status='closed' AND champion_status IS NULL AND champion_r IS NOT NULL",
    }
    out = {}
    with store._conn() as conn:
        for k, cond in q.items():
            row = conn.execute(f"SELECT COUNT(*) AS n FROM shadow_signals WHERE {cond}").fetchone()
            out[k] = int((dict(row) if row else {}).get("n") or 0)
    return out


def _pct_ret(row, key="r20"):
    """把以 R 為單位的前瞻報酬換成百分比：r × 規劃風險 ÷ 實際進場價（隔日開盤＝entry×(1+gap%)）。"""
    try:
        rv = row.get(key)
        if rv is None:
            return None
        entry, stop = float(row["entry"]), float(row["stop"])
        fill = entry * (1 + float(row.get("gap_pct") or 0) / 100)
        return float(rv) * abs(entry - stop) / fill * 100 if fill > 0 else None
    except Exception:
        return None


def matched_control_report(rows=None, horizon="r20") -> dict:
    """★ 2026-11-12（S-P1-04）選股優勢：做多候選的前瞻報酬 − 其配對對照（同日同產業同規模、成交金額最接近）
    的平均，與「− 同日隨機對照平均」並列。訊號日 × 股票二維拔靴 95% 區間（S-P1-05）。只用影子資料、只評估。
    只有樣本外（2026-11-13 起）才產生配對對照，之前的列沒有，不回填。"""
    if rows is None:
        ensure_table()
        from state_store import store
        with store._conn() as conn:
            rows = [dict(x) for x in conn.execute(
                "SELECT bar_date, ticker, kind, direction, entry, stop, gap_pct, r10, r20, features FROM shadow_signals "
                "WHERE kind IN ('candidate','control','matched') AND bar_date>=?", ("2026-11-13",)).fetchall()]
    import experiments as _ex
    cands, ctrl_by_day, match_of = [], {}, {}
    for r in rows:
        try:
            f = r.get("features")
            f = json.loads(f) if isinstance(f, str) else (f or {})
        except Exception:
            f = {}
        pr = _pct_ret(r, horizon)
        if pr is None:
            continue
        if r.get("kind") == "candidate" and r.get("direction") == "buy":
            cands.append((r["bar_date"], r["ticker"], pr))
        if r.get("kind") == "control":
            ctrl_by_day.setdefault(r["bar_date"], []).append(pr)
        for c in f.get("match_for") or []:
            match_of.setdefault((r["bar_date"], c), []).append(pr)
    vm, vr = [], []
    for d, t, pr in cands:
        m = match_of.get((d, t))
        if m:
            vm.append((d, t, pr - sum(m) / len(m)))
        if ctrl_by_day.get(d):
            vr.append((d, t, pr - sum(ctrl_by_day[d]) / len(ctrl_by_day[d])))
    def _summ(obs):
        if not obs:
            return {"n": 0, "n_days": 0, "mean_pct": None, "ci95_twoway": [None, None]}
        lo, hi = _ex._twoway_ci(obs)
        return {"n": len(obs), "n_days": len({o[0] for o in obs}),
                "mean_pct": round(sum(o[2] for o in obs) / len(obs), 3), "ci95_twoway": [lo, hi]}
    return {"horizon": horizon, "vs_matched": _summ(vm), "vs_random_controls": _summ(vr),
            "note": ("做多候選隔日開盤進場後的報酬（%）減去對照組。配對對照＝同日同產業同規模、成交金額最接近的 3 檔"
                     "非訊號股；隨機對照＝同日隨機抽樣的非訊號股。7 年重播中隨機對照會把超額報酬高估約 0.44 個百分點，"
                     "以配對對照為準。只用模擬資料，不是實際成交。")}


def cohort_report() -> dict:
    """★ 2026-11-12（S-P1-03／S-P1-08）候選漏斗與成熟度，多空分開。

    漏斗：candidate（通過規則的全部候選）≠ sent（真的排入推播）≠ delivered（Telegram 確認收下）。
    歷史匯出中 2,511 筆候選只有 15 筆 sent——把全部候選的統計當成「線上實績」會有嚴重的選擇偏差，
    因此各階段筆數必須並列；且沒有券商成交回報，任何一階都不能稱為實際成交。

    成熟度：停損通常比停利先發生，早結案的樣本偏向虧損；只看「已結案」會有截尾偏差。
    因此另算「成熟訊號日」（該日候選已全部離開 pending）的平均 R，並揭露未成熟比例。"""
    ensure_table()
    from state_store import store
    with store._conn() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT bar_date, direction, status, sent, delivered, r_multiple, champion_r, champion_status, config_ver "
            "FROM shadow_signals WHERE kind='candidate'").fetchall()]
    out = {}
    # ★ 2026-11-12（S-P0-04）依策略版本分組：規則一改，候選的母體就不同（例如 3bdbd319e626 起不再有放空、
    # 做多只剩週線偏多且 ≥75 分者），不同版本的平均不可直接合併解讀。NULL＝該列產生時尚未記錄版本。
    by_ver = {}
    for r in rows:
        v = by_ver.setdefault((r.get("config_ver") or "未記錄", r.get("direction")), {"n": 0, "rs": [], "days": set()})
        v["n"] += 1
        v["days"].add(r.get("bar_date"))
        if r.get("champion_status") == "final" and r.get("champion_r") is not None:
            v["rs"].append(r["champion_r"])
    out["by_config_ver"] = [
        {"config_ver": k[0], "direction": k[1], "candidates": v["n"], "signal_days": len(v["days"]),
         "first_day": min(v["days"]) if v["days"] else None, "last_day": max(v["days"]) if v["days"] else None,
         "champion_final": len(v["rs"]),
         "champion_avg_r": round(sum(v["rs"]) / len(v["rs"]), 3) if v["rs"] else None}
        for k, v in sorted(by_ver.items(), key=lambda kv: (min(kv[1]["days"]) if kv[1]["days"] else "", kv[0][1] or ""))]
    for d in ("buy", "sell"):
        rs = [r for r in rows if r.get("direction") == d]
        by_day = {}
        for r in rs:
            by_day.setdefault(r["bar_date"], []).append(r)
        mature_days = [k for k, v in by_day.items() if all(x.get("status") != "pending" for x in v)]
        closed = [r for r in rs if r.get("status") == "closed" and r.get("r_multiple") is not None]
        mature = [r for r in closed if r["bar_date"] in set(mature_days)]
        avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
        out[d] = {
            "funnel": {"candidate": len(rs),
                       "sent": sum(1 for r in rs if r.get("sent") == 1),
                       "delivered": sum(1 for r in rs if r.get("delivered") == 1),
                       "closed_valid_r": len(closed),
                       "champion_final": sum(1 for r in rs if r.get("champion_status") == "final"),
                       "filled": None},          # 無券商成交回報：誠實留空，不以推播數代替
            "maturity": {"signal_days": len(by_day), "mature_days": len(mature_days),
                         "pending": sum(1 for r in rs if r.get("status") == "pending"),
                         "pending_share": round(sum(1 for r in rs if r.get("status") == "pending") / len(rs), 3) if rs else None,
                         "unresolved": sum(1 for r in rs if r.get("status") == "unresolved"),
                         "all_closed_avg_r": avg([r["r_multiple"] for r in closed]),
                         "mature_days_avg_r": avg([r["r_multiple"] for r in mature]),
                         "n_mature": len(mature)}}
    try:
        out["selection_edge"] = matched_control_report()
    except Exception as e:
        out["selection_edge"] = {"error": str(e)[:200]}
    out["note"] = ("candidate≠sent≠delivered≠實際成交（無券商回報，filled 留空）。"
                   "all_closed_avg_r 含未成熟訊號日（先停損者先結案，偏悲觀或偏樂觀皆可能）；"
                   "mature_days_avg_r 只計候選已全部離開 pending 的訊號日。")
    return out


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
def _delivery_summary(sent, delivered, no_field=None, unconfirmed=None) -> dict:
    """★ 2026-11-11 SH11／2026-11-12 分母修正：推播送達對帳。delivered=1 只代表 provider 至少一
    目標回 200（已確認），**未確認非一定失敗**；confirmed_rate 是『已確認比例』，不等於真實送達率。

    2026-11-12：分母正名。delivered 欄 2026-11-06 才新增，更早的推播結構上**無對帳欄位**
    （NULL），永遠不可能被確認；原本把它們一起算進分母，會讓已確認率長期偏低並誤報告警。
    改為：可對帳母體(reconcilable)＝已確認(delivered)＋可對帳但未確認(unconfirmed)；
    無對帳欄位(no_field)的舊列**不納入比率**、單獨揭露，不回填、不假裝已送達。
    未帶新參數時沿用舊行為（向後相容）。"""
    s = int(sent or 0); d = int(delivered or 0)
    nf = int(no_field or 0)
    unc = int(unconfirmed) if unconfirmed is not None else max(0, s - d - nf)
    recon = d + unc
    return {"sent": s, "delivered": d, "unknown": max(0, s - d),
            "no_field": nf, "unconfirmed": unc, "reconcilable": recon,
            "confirmed_rate": round(d / recon * 100, 1) if recon else None,
            "rate": round(d / recon * 100, 1) if recon else None,     # 保留相容欄位
            "note": "confirmed_rate＝可對帳母體中『平台至少一目標回200』的已確認比例，非真實送達率；"
                    "未確認(unknown)不等於失敗；no_field＝2026-11-06 前無對帳欄位的舊紀錄，不納入分母。"}


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
        # ★ 2026-11-06：sent→delivered 送達對帳（候選列）。sent=1＝排入推播；delivered=1＝Telegram 真收下。
        # ★ 2026-11-12 對帳分母正名：把「排入推播」拆成三類——已確認(delivered=1)、
        # 可對帳但未確認(delivered=0)、無對帳欄位(delivered IS NULL，2026-11-06 前的舊紀錄，
        # 結構上永遠無法確認)。只有前兩類構成可對帳母體，舊紀錄不得混入分母造成誤報。
        _dlv = dict(conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN sent=1 THEN 1 ELSE 0 END),0) AS sent, "
            "COALESCE(SUM(CASE WHEN sent=1 AND delivered=1 THEN 1 ELSE 0 END),0) AS delivered, "
            "COALESCE(SUM(CASE WHEN sent=1 AND delivered=0 THEN 1 ELSE 0 END),0) AS unconfirmed, "
            "COALESCE(SUM(CASE WHEN sent=1 AND delivered IS NULL THEN 1 ELSE 0 END),0) AS no_field "
            "FROM shadow_signals WHERE kind='candidate'").fetchone() or {})
        # ★ 2026-11-08：特徵 schema 版本分布（偵測漂移／標示未標版的舊資料）
        _fsv = [dict(x) for x in conn.execute(
            "SELECT COALESCE(feat_schema_ver,'(未標版)') AS ver, COUNT(*) AS n "
            "FROM shadow_signals WHERE kind='candidate' GROUP BY feat_schema_ver ORDER BY n DESC").fetchall()]
        # ★ 2026-11-09 Shadow 雙軌：Champion（分批）vs Research（單次）淨 R 對照，依方向分開（不混多空）。
        _champ = [dict(x) for x in conn.execute(
            "SELECT direction, COUNT(*) AS n, "
            "AVG(r_multiple) AS research_avg_r, AVG(champion_r) AS champion_avg_r "
            "FROM shadow_signals WHERE kind='candidate' AND status='closed' AND champion_r IS NOT NULL "
            "GROUP BY direction").fetchall()]
        # ★ 2026-11-12（S-P0-01）：Champion 軌尚未定案／無法定案的筆數，與定案值分開揭露。
        _champ_state = [dict(x) for x in conn.execute(
            "SELECT COALESCE(champion_status,'(舊值待重驗)') AS st, COUNT(*) AS n FROM shadow_signals "
            "WHERE kind='candidate' AND status='closed' AND r_multiple IS NOT NULL "
            "GROUP BY champion_status").fetchall()]
    # 特徵覆蓋率（每種來源各自算）
    cov, cnt, _distinct = {}, {}, {}
    for x in sample:
        try:
            f = json.loads(x["features"] or "{}")
        except Exception:
            f = {}
        cnt[x["kind"]] = cnt.get(x["kind"], 0) + 1
        for k in f:
            cov.setdefault(k, {}).setdefault(x["kind"], 0)
            cov[k][x["kind"]] += 1
            if f[k] is not None and len(_distinct.setdefault(k, set())) < 3:
                try:
                    _distinct[k].add(f[k] if isinstance(f[k], (str, int, float, bool)) else str(f[k]))
                except Exception:
                    pass
    coverage = []
    for k, d in cov.items():
        # ★ 2026-11-12（S-P1-07）：抽樣中只有一種取值的特徵（如歷史上恆為 0 的 news_neg）沒有
        # 鑑別力，逐一檢定只會增加虛假比較數。標示 constant 供研究端隔離；不刪原始資料。
        coverage.append({"key": k, "pct": {kd: round(d.get(kd, 0) / cnt[kd] * 100) for kd in cnt},
                         "constant": len(_distinct.get(k, ())) <= 1})
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
    _sent = int(_dlv.get("sent") or 0); _deliv = int(_dlv.get("delivered") or 0)
    _unconf = int(_dlv.get("unconfirmed") or 0); _nofield = int(_dlv.get("no_field") or 0)
    delivery = _delivery_summary(_sent, _deliv, _nofield, _unconf)
    # ★ 2026-11-12：只有「可對帳母體內未確認」才可能是真的沒送達；無對帳欄位的舊紀錄
    # 結構上無法確認，不再混進告警（原本會長期誤報），改由 KPI 誠實分列。
    if _unconf:
        alerts.append(f"推播送達對帳：可對帳 {_deliv + _unconf} 筆中有 {_unconf} 筆未確認送達"
                      f"（Telegram 未回報成功，可能實際未送達）"
                      + (f"；另有 {_nofield} 筆舊紀錄無對帳欄位，不納入對帳" if _nofield else ""))
    champion_edge = [{"direction": c.get("direction"), "n": int(c.get("n") or 0),
                      "research_avg_r": round(c["research_avg_r"], 3) if c.get("research_avg_r") is not None else None,
                      "champion_avg_r": round(c["champion_avg_r"], 3) if c.get("champion_avg_r") is not None else None}
                     for c in _champ]
    return {"summary": summary, "per_day": per_day, "runs": runs, "meta": meta, "oldest_pending": oldest,
            "coverage": coverage[:60], "coverage_n": cnt, "recent": recent, "alerts": alerts,
            "delivery": delivery, "feature_schema": {"current": FEATURE_SCHEMA_VERSION, "dist": _fsv},
            "champion_edge": champion_edge,
            "champion_state": {x["st"]: int(x["n"] or 0) for x in _champ_state},
            "cohorts": _safe_cohorts(),
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
    fkeys = [k for k, _ in sorted(keys.items(), key=lambda z: -z[1])][:500]   # 2026-10-16：上限 150→500，幾乎匯出全部特徵
    base = ["id", "bar_date", "ticker", "code", "name", "kind", "direction", "score", "entry", "stop", "tp1", "tp2",
            "tp3", "size_cat", "sector", "sent", "status", "result", "exit_price", "exit_date", "r_multiple", "champion_r", "champion_status", "champion_exit_date", "champion_r_prov", "champion_r_legacy",
            "config_ver", "champion_sim_ver", "feat_schema_ver", "strategy_name",
            "mfe_r", "mae_r", "bars_held", "gap_pct", "r1", "r5", "r10", "r20", "mae_bars", "mfe_bars", "tp1_before_sl", "created_at"]
    # 2026-10-16：加上離線分析常用的衍生欄位，省得每次自己算
    WIN = ("tp1", "tp2", "tp3")
    derived = ["win", "is_stop", "hold_days", "reject_reason"]
    def _derive(x, f):
        res = x.get("result")
        win = 1 if res in WIN else (0 if res == "sl" else "")
        is_stop = 1 if res == "sl" else 0
        hold = ""
        try:
            if x.get("exit_date") and x.get("bar_date"):
                hold = (datetime.strptime(x["exit_date"][:10], "%Y-%m-%d")
                        - datetime.strptime(x["bar_date"][:10], "%Y-%m-%d")).days
        except Exception:
            hold = ""
        rej = REASONS.get(f.get("reject"), f.get("reject")) if f.get("reject") else ""
        return [win, is_stop, hold, rej]
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(base + derived + ["f_" + k for k in fkeys])
    for x, f in zip(rows, parsed):
        w.writerow([x.get(k) for k in base] + _derive(x, f) + [f.get(k) for k in fkeys])
    return out.getvalue(), len(rows)


def verify_backup() -> dict:
    """★ 2026-11-04（Phase 1 維運：備份還原測試）——把備份匯出的 CSV 重新解析、載入一個臨時
    記憶體 SQLite，確認：筆數與資料庫一致、欄位完整、每一列都能無錯誤還原。這證明「備份檔真的
    還原得回來」，不是只產生了一個檔案。回傳 {ok, match, rows_db, rows_csv, rows_restored,
    columns, size_bytes, checked_at}；還原過程出錯回 {ok:False, error}。純唯讀、不動正式資料。"""
    import sqlite3
    ensure_table()
    from state_store import store
    try:
        with store._conn() as conn:
            rows_db = (conn.execute("SELECT COUNT(*) AS n FROM shadow_signals").fetchone() or {"n": 0})["n"]
        body, n = export_csv()
        rdr = csv.reader(io.StringIO(body.lstrip("﻿")))
        header = next(rdr, None) or []
        data = [r for r in rdr]
        rows_csv = len(data)
        restored = 0
        mem = sqlite3.connect(":memory:")
        try:
            cols = ", ".join('"%s" TEXT' % c for c in header)
            mem.execute(f"CREATE TABLE bak ({cols})")
            ph = ",".join(["?"] * len(header))
            for r in data:
                if len(r) == len(header):
                    mem.execute(f"INSERT INTO bak VALUES ({ph})", r)
            restored = mem.execute("SELECT COUNT(*) FROM bak").fetchone()[0]
        finally:
            mem.close()
        match = (rows_db == rows_csv == restored)
        return {"ok": True, "match": match, "rows_db": rows_db, "rows_csv": rows_csv,
                "rows_restored": restored, "columns": len(header),
                "size_bytes": len(body.encode("utf-8")),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "note": "備份匯出→解析→載入臨時 SQLite 的還原測試；三處筆數一致且無錯誤即代表備份可還原。"}
    except Exception as e:
        logger.error(f"verify_backup: {e}", exc_info=True)
        return {"ok": False, "error": "還原驗證失敗，請檢查日誌"}
