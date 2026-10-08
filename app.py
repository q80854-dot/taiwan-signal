"""
app.py — Flask 主應用 v1.2
修正：改用 send_from_directory 繞過 Jinja2 解析 dashboard.html
"""
import sys, os, signal, logging, threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
from config import SYSTEM, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_FREE_CHANNEL, TELEGRAM_PAID_CHANNEL

logging.basicConfig(
    level=getattr(logging, SYSTEM["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ★ 2026-10-06：日誌一律遮蔽 Telegram Bot Token（原本 Webhook 網址會連 token 一起寫進日誌）
import re as _re
class _RedactFilter(logging.Filter):
    _pat = _re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}")
    def filter(self, record):
        try:
            msg = record.getMessage()
            if self._pat.search(msg):
                record.msg = self._pat.sub("<bot-token>", msg)
                record.args = ()
        except Exception:
            pass
        return True
for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactFilter())

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024     # 請求內容上限 256KB，避免被塞超大請求
app.config["JSON_AS_ASCII"] = False

# 2026-10-07：指標算不出來時（例如新上市 ETF 的 K 線不足）會產生 NaN，Python 會把它輸出成 JSON 裡的
# 「NaN」，但那不是合法 JSON，瀏覽器 JSON.parse 會直接失敗，整個個股頁就變成「沒有任何資料」
# （00631L、0050 就是這樣）。這裡全站統一把 NaN／無限大轉成 null。
import math as _math
from flask.json.provider import DefaultJSONProvider as _DJP


def _json_clean(o):
    if isinstance(o, float):
        return o if _math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _json_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_clean(v) for v in o]
    try:                                    # numpy 純量（float32 等）
        import numpy as _np
        if isinstance(o, _np.generic):
            return _json_clean(o.item())
        if isinstance(o, _np.ndarray):
            return _json_clean(o.tolist())
    except Exception:
        pass
    return o


class _SafeJSONProvider(_DJP):
    def dumps(self, obj, **kwargs):
        return super().dumps(_json_clean(obj), **kwargs)


app.json = _SafeJSONProvider(app)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    TZ_TAIPEI  = pytz.timezone("Asia/Taipei")
    scheduler  = BackgroundScheduler(timezone=TZ_TAIPEI)
    SCHEDULER_OK = True
except ImportError:
    logger.warning("APScheduler 未安裝，排程功能停用")
    SCHEDULER_OK = False

# ── 排程任務 ──
def job_morning_brief():
    # ★ 新增：2026-09-25——同 job_daily_scan 的修正：國定假日當天沒有開盤，
    # 早盤摘要照常推播會誤導使用者以為今天有交易，見 config.py
    # TW_MARKET_HOLIDAYS 的說明。
    from data_fetcher import get_market_session
    if get_market_session().get("session") == "holiday":
        logger.info("⏰ 早盤摘要：今天是國定假日休市，跳過本次推播")
        return
    logger.info("⏰ 早盤摘要")
    try:
        from data_fetcher import fetch_market_overview
        from telegram_bot import send_morning_brief
        send_morning_brief(fetch_market_overview())
    except Exception as e: logger.error(f"job_morning_brief: {e}")

def job_premarket_cache_refresh():
    # ★ 新增：2026-09-24——見 data_fetcher.premarket_cache_refresh() 說明。
    # 排在 08:00（早於 09:00 開盤、晚於前一天收盤資料在 yfinance 定案的時間），
    # 讓全市場K線快取在盤前就補到最新並驗證過一次，16:30 正式掃描時才能真正
    # 吃到現成快取、不用重新對外部 API 下載整段歷史。
    # ★ 新增：2026-09-25——國定假日當天沒有新的收盤資料需要補，跳過避免浪費
    # 資源，見 config.py TW_MARKET_HOLIDAYS 的說明。
    from data_fetcher import get_market_session
    if get_market_session().get("session") == "holiday":
        logger.info("⏰ 早盤前K線快取更新：今天是國定假日休市，跳過本次更新")
        return
    logger.info("⏰ 早盤前K線快取更新與完整性檢查")
    try:
        from data_fetcher import premarket_cache_refresh
        from telegram_bot import send_alert
        stats = premarket_cache_refresh()
        msg = (f"🌅 早盤前資料檢查完成\n"
               f"K線快取更新：{stats['success']}/{stats['total']} 檔成功"
               + (f"（{stats['failed_count']} 檔失敗）" if stats['failed_count'] else "") + "\n"
               f"抽樣完整性驗證：{stats['validated']} 檔，{stats['validated_ok']} 檔一致")
        if stats["mismatches"]:
            msg += f"\n⚠️ {len(stats['mismatches'])} 檔快取與最新資料不一致，已記錄於系統日誌，今日訊號請留意：" \
                   + "、".join(m["ticker"] for m in stats["mismatches"][:10])
        send_alert(msg, "warning" if stats["mismatches"] else "info")
    except Exception as e:
        logger.error(f"job_premarket_cache_refresh: {e}", exc_info=True)

def job_daily_scan():
    # ★ 新增：2026-09-25——使用者反映「今天（中秋節）沒開盤」，稽核發現排程
    # 的 CronTrigger 只設 day_of_week="mon-fri"，平日國定假日一樣會照常觸發
    # 全市場掃描（見 config.py TW_MARKET_HOLIDAYS、data_fetcher.py
    # get_market_session() 的說明）。這裡在真正開始掃描前先查一次是否為
    # 國定假日休市日，是的話直接跳過，不浪費一次全市場掃描的資源，也避免
    # watchdog 誤以為「今天沒掃描」而發假警報（job_scan_watchdog 只看
    # last_scan_at 的日期，這裡跳過的話 watchdog 仍可能誤報，需要同步處理，
    # 見 job_scan_watchdog 的對應修正）。
    from data_fetcher import get_market_session
    session = get_market_session()
    if session.get("session") == "holiday":
        logger.info(f"⏰ 全市場掃描：今天是國定假日休市（{session.get('taipei_time')}），跳過本次掃描")
        return
    logger.info("⏰ 全市場掃描")
    try:
        # 掃描前先確認官方日行情已更新到今天（沒有就重抓一次）；仍沒更新時，最後一根 K 棒改用 MIS 盤後定案資料
        try:
            from stock_universe import refresh_if_date_stale, get_official_bar
            refresh_if_date_stale()
            _ob = get_official_bar("2330")
            logger.info(f"掃描前官方日 K 檢查：2330 → {_ob}")
        except Exception as _e:
            logger.warning(f"掃描前官方日 K 檢查失敗：{_e}")
        from scanner import scanner
        scanner.run_daily_scan()
        try:
            import data_fetcher as _df
            logger.info(f"掃描完成｜官方日 K 套用統計：{_df._obar_stats}")
        except Exception:
            pass
    except Exception as e: logger.error(f"job_daily_scan: {e}", exc_info=True)

def job_premarket_scan():
    """盤前全市場掃描（平日 08:20）：與盤後 16:30 掃描互補——納入隔夜資訊與最新處置／暫停名單，並補抓前一晚失敗的標的。"""
    from data_fetcher import get_market_session
    if get_market_session().get("session") == "holiday":
        logger.info("⏰ 盤前掃描：今天是國定假日休市，跳過")
        return
    logger.info("⏰ 盤前全市場掃描")
    try:
        from scanner import scanner
        scanner.run_daily_scan(mode="premarket")
    except Exception as e:
        logger.error(f"job_premarket_scan: {e}", exc_info=True)

def job_intraday_check():
    # ★ 新增：2026-09-16——使用者質疑「盤中完全沒有掃描」，稽核後認為「盤中不找
    # 新訊號」本身沒錯（波段策略本來就該用收盤後定案的日K找新進場點，盤中日K還
    # 沒走完，提早用殘缺的K棒找訊號反而容易誤判/來回洗），但「已經推播出去、使用者
    # 正在追蹤的訊號」如果盤中就已經到價，不該悶不吭聲等到隔天才讓使用者知道——
    # 詳細原因見 scanner.py TWScanEngine.__init__ 裡的說明。這裡只做輕量的價格
    # 比對+警示（見 check_intraday_price_alerts），不是另一次全市場掃描，資源成本
    # 很小（只查詢目前追蹤中的幾檔，通常 <5 檔）。
    # ★ 新增：2026-09-25——同 job_daily_scan 的修正，國定假日（平日）要跳過，
    # 見 config.py TW_MARKET_HOLIDAYS 的說明。
    from data_fetcher import get_market_session
    session = get_market_session()
    if session.get("session") == "holiday":
        logger.info(f"⏰ 盤中安全網檢查：今天是國定假日休市，跳過本次檢查")
        return
    import realtime
    if not realtime.market_open():
        return
    logger.info("⏰ 盤中安全網檢查")
    try:
        from scanner import scanner
        scanner.check_intraday_price_alerts()
    except Exception as e: logger.error(f"job_intraday_check: {e}", exc_info=True)

def _append_index_series(store, snap):
    """把當日指數走勢逐筆累積進 meta index_intraday（每輪快照一點，約 5 分鐘一點），供網頁畫當日走勢圖。隔日自動重置。"""
    import realtime
    today = realtime.now_tpe().strftime("%Y-%m-%d")
    cur = store.get_meta("index_intraday") or {}
    if cur.get("date") != today:
        cur = {"date": today, "series": {}, "prev": {}}
    for k, v in (snap.get("indices") or {}).items():
        if not v or v.get("date") != today or not v.get("price"):
            continue
        pts = cur["series"].setdefault(k, [])
        t = str(v.get("time") or "")[:5]
        if pts and pts[-1][0] == t:
            pts[-1][1] = v["price"]
        else:
            pts.append([t, v["price"]])
        cur["prev"][k] = v.get("prev_close")
    if cur["series"]:
        store.set_meta("index_intraday", cur)


def _take_snapshot(save_live=True, save_bars=False):
    """抓全市場 MIS 快照。save_live：存盤中快照；save_bars：把 13:33 之後的定案 OHLCV 另存（供收盤掃描當官方日 K 用）。"""
    import realtime
    from stock_universe import build_full_universe
    from state_store import store
    _uni = build_full_universe()
    snap = realtime.snapshot(_uni)
    try:
        snap["quality"] = realtime.quality_check(snap["quotes"], _uni)
        _ql = snap["quality"]
        if (_ql.get("mismatch_rate") or 0) > 0.05 or _ql.get("out_of_limit", 0) > 5:
            logger.warning(f"盤中快照品質異常：{_ql}")
    except Exception as _e:
        logger.warning(f"quality_check: {_e}")
    if save_live and snap["n_ok"] > 0:
        store.set_meta("intraday_snapshot", snap)
        if any((v or {}).get("price") for v in (snap.get("indices") or {}).values()):
            try:
                store.set_meta("index_last", snap["indices"])      # 最後一次成功的指數（供未登入首頁在來源失敗時顯示）
            except Exception as _e:
                logger.warning(f"index_last: {_e}")
        try:
            _append_index_series(store, snap)
        except Exception as _e:
            logger.warning(f"index_intraday: {_e}")
    logger.info(f"盤中快照：要求 {snap['n_req']} 檔、取得 {snap['n_ok']} 檔（完整度 {snap.get('completeness')}）、耗時 {snap['secs']}s、廣度 {snap.get('breadth')}")
    if snap.get("completeness") is not None and snap["completeness"] < 0.9:
        logger.warning(f"盤中快照完整度偏低（{snap['completeness']}），來源可能限流或不完整")
    if snap["n_ok"] == 0:
        logger.warning(f"盤中快照：證交所 MIS 全部抓不到（{realtime.status().get('last_error')}）")
    _nn = realtime.now_tpe()
    if save_bars and snap["n_ok"] > 0 and _nn.hour * 60 + _nn.minute >= 13 * 60 + 33:   # 收盤定案後才存，盤中不存
        today = _nn.strftime("%Y-%m-%d")
        bars = {c: [q[7], q[8], q[9], q[0], q[2] or 0] for c, q in snap["quotes"].items()
                if len(q) > 9 and q[4] == today and q[0] and q[7] and q[8] and q[9]}
        if len(bars) >= 0.8 * snap["n_ok"]:
            store.set_meta("day_bars_mis", {"date": today, "at": snap["at"], "n": len(bars), "bars": bars})
            logger.info(f"當日定案 OHLCV 已存檔（MIS）：{today} {len(bars)} 檔")
        else:
            logger.warning(f"當日定案 OHLCV 檔數過少（{len(bars)}/{snap['n_ok']}），不存檔")
    return snap


def job_intraday_snapshot():
    """盤中每 5 分鐘：用證交所 MIS（官方即時）抓全市場報價，整份存進資料庫（meta intraday_snapshot）。
    13:33 之後的那一輪（13:35）同時存當日定案 OHLCV。"""
    import realtime
    if not realtime.market_open():
        return
    from data_fetcher import get_market_session
    if get_market_session().get("session") == "holiday":
        return
    try:
        _n = realtime.now_tpe()
        _take_snapshot(save_live=True, save_bars=(_n.hour * 60 + _n.minute >= 13 * 60 + 33))
    except Exception as e:
        logger.error(f"job_intraday_snapshot: {e}", exc_info=True)


def job_final_bars():
    """收盤後補抓當日定案 OHLCV（13:40、14:10 各一次，防 13:35 那輪失敗）。"""
    import realtime
    n = realtime.now_tpe()
    if n.weekday() > 4:
        return
    from data_fetcher import get_market_session
    if get_market_session().get("session") == "holiday":
        return
    try:
        _take_snapshot(save_live=True, save_bars=True)
    except Exception as e:
        logger.error(f"job_final_bars: {e}", exc_info=True)


def job_market_extras():
    """證交所補充資料：交易限制旗標（暫停/變更交易、停資停券…）與融券／借券賣出餘額歷史。"""
    try:
        import market_extras
        from state_store import store
        f = market_extras.get_flags(force=True)
        logger.info(f"補充資料旗標：{market_extras.status().get('counts')} 錯誤 {f.get('errors')}")
        market_extras.update_short_hist(store)
        import taifex
        _d = taifex.update_hist(store, taifex.get_taifex(force=True))
        logger.info(f"期交所籌碼已更新：{_d}")
    except Exception as e:
        logger.error(f"job_market_extras: {e}", exc_info=True)


def job_refresh_universe_if_stale():
    """官方日行情的日期若落後（例如 16:00 抓到時證交所還沒更新），收盤後每小時補抓。"""
    try:
        from stock_universe import refresh_if_date_stale
        if refresh_if_date_stale():
            logger.info("官方日行情日期過期，已重新下載")
    except Exception as e:
        logger.error(f"job_refresh_universe_if_stale: {e}")


def job_collect_inst():
    """收盤後把當天三大法人買賣超存進資料庫（T86 約 16:00 後陸續公布，晚上再補一次）。"""
    try:
        from data_fetcher import backfill_inst_daily
        from state_store import store
        backfill_inst_daily(store, want_days=30)
    except Exception as e:
        logger.warning(f"job_collect_inst: {e}")


def job_refresh_universe():
    logger.info("⏰ 品種清單更新")
    try:
        from stock_universe import refresh_universe_daily
        refresh_universe_daily()
    except Exception as e: logger.error(f"job_refresh_universe: {e}")

def job_scan_watchdog():
    # ★ 新增：2026-09-16——稽核時發現過一次長達 5.5 天（2026-09-03 17:00 UTC～
    # 2026-09-09 03:34 UTC）完全無 log 的服務停擺，期間 3 個交易日完全沒有掃描
    # /推播，而且是使用者自己發現的，系統本身完全沒有機制會主動通知——這正是
    # 「資訊更新速度」問題裡最嚴重的一種：不是慢，是整個停了都不知道。這裡在
    # 每日 16:30 掃描理論上早該完成的時間點（17:00）自我檢查一次：如果
    # scanner.last_scan_at 的日期不是今天，代表當天的排程掃描沒有實際跑完
    # （不管是 process 被平台重啟、程式卡死、還是排程器本身失效），主動推播
    # 一則警示給機主，讓這類問題幾分鐘內就能被發現，不用再等好幾天才注意到。
    # 註：這個 watchdog 本身仍跑在同一個 process 內，如果整個 process 直接
    # 掛掉（像上次那樣），watchdog 也不會觸發——那種情況要靠 Render
    # healthCheckPath 設定讓平台自動偵測並重啟，兩者互補、缺一不可。
    # ★ 新增：2026-09-25——同 job_daily_scan 的修正：國定假日當天 job_daily_scan
    # 會主動跳過（見上方說明），這裡如果不一起跳過，watchdog 會誤以為「今天
    # 掃描沒跑」而發假警報。用同一份 config.py TW_MARKET_HOLIDAYS 判斷。
    from data_fetcher import get_market_session
    if get_market_session().get("session") == "holiday":
        logger.info("⏰ 排程健康檢查：今天是國定假日休市，今日本就不會有掃描，跳過本次檢查")
        return
    logger.info("⏰ 排程健康檢查")
    try:
        from scanner import scanner
        from telegram_bot import send_alert
        today = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
        last  = (scanner.last_scan_at or "")[:10]
        if last != today:
            msg = (
                f"🚨 排程異常：預定 16:30 的每日掃描今天（{today}）似乎沒有執行\n"
                f"最後一次掃描時間：{scanner.last_scan_at or '從未執行過'}\n"
                f"請檢查 Render 服務狀態，或至網站手動觸發「立即掃描」"
            )
            logger.error(f"job_scan_watchdog: 今日掃描未執行，last_scan_at={scanner.last_scan_at}")
            send_alert(msg, "error")
        else:
            logger.info(f"job_scan_watchdog: 今日掃描已正常執行 last_scan_at={scanner.last_scan_at}")
    except Exception as e:
        logger.error(f"job_scan_watchdog: {e}", exc_info=True)

def job_pre_scan_restart():
    # ★ 新增：2026-09-18——2026-09-03 已發生過一次、2026-09-18 又重演的同一種
    # 故障：本服務是 Render 0.5c/512MB 方案，即使 data_fetcher._cache 已經加了
    # 400 筆上限（見 2026-09-03 那次修正），實測用 Render memory_usage 指標
    # 確認：2026-09-18 08:25（當天掃描都還沒開始）常駐記憶體就已經
    # 512,888,830 bytes，而 512MB 方案的實際上限是 536,870,900 bytes——閒置時
    # 就已經用掉約 95%，掃描一開始再疊加（yfinance/pandas 大量 DataFrame 物件
    # 造成的 CPython/glibc malloc 記憶體碎片化，不是 _cache 字典本身能解釋的，
    # gc.collect() 也無法讓 glibc 把已釋放但碎片化的記憶體真的還給作業系統），
    # 15 分鐘內就衝破上限，Render 平台直接把整個 container 判定超限、強制重啟
    # ——這次是 08:45（掃描才跑到 18 批中的第 10~11 批），當天所有已找到的
    # 訊號（含好幾檔 A 級）全部遺失、沒有推播到 Telegram，使用者只收到 17:00
    # job_scan_watchdog 事後才發出的異常警示。
    #
    # 治本地重寫整個掃描的記憶體使用方式風險太高（本機沒有能重現 880 檔即時
    # 行情負載的測試環境可驗證改動是否安全），這裡改用業界常見、風險低很多的
    # 作法：讓 gunicorn 這個唯一的 worker process，在每天掃描「開始之前」
    # （16:15——refresh_universe 16:00 已跑完、daily_scan 16:30 還沒開始的
    # 安全空檔）自己送 SIGTERM 結束自己。gunicorn master 偵測到 worker 程序
    # 消失後會自動生一個全新 worker（跟 gunicorn 內建 --timeout/--max-requests
    # 的自我回收機制原理完全一樣），讓 16:30 的掃描從乾淨的記憶體基準（實測
    # 重啟後約 98MB）開始跑，而不是從已經逼近上限的 ~490MB 開始，大幅拉開
    # OOM 前的安全餘裕。若 16:15 當下剛好有掃描正在進行中（例如使用者從
    # 網站手動觸發「立即掃描」），則跳過本次重啟，避免腰斬正在跑的掃描。
    logger.info("⏰ 掃描前記憶體重置檢查")
    try:
        from scanner import scanner
        if scanner.is_scanning:
            logger.warning("job_pre_scan_restart: 目前有掃描正在進行中，跳過本次重啟")
            return
        logger.info("job_pre_scan_restart: 主動重啟 worker 以重置記憶體基準（16:30 掃描前）")
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception as e:
        logger.error(f"job_pre_scan_restart: {e}", exc_info=True)

def setup_scheduler():
    if not SCHEDULER_OK: return
    # ★ 新增：2026-09-24——早盤前K線快取更新+完整性檢查，見 job_premarket_cache_refresh()
    # 說明。排在 08:00，早於 08:45 的早盤摘要與 09:00 開盤，晚於前一天收盤資料在
    # yfinance 定案的時間。
    scheduler.add_job(job_premarket_cache_refresh, CronTrigger(hour=8, minute=0, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="premarket_cache_refresh", replace_existing=True)
    scheduler.add_job(job_premarket_scan,   CronTrigger(hour=8,  minute=20, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="premarket_scan",   replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_morning_brief,    CronTrigger(hour=8,  minute=45, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="morning_brief",    replace_existing=True)
    # ★ 新增：2026-09-16——盤中安全網，查已追蹤訊號是否到價，見 job_intraday_check() 說明。
    # ★ 調整：2026-09-19——使用者反映盤中只查4次（10/11/12/13點整）太少、涵蓋不到
    # 9:00-10:00開盤這一小時、也涵蓋不到13:00-13:30收盤前這半小時，而且兩次檢查
    # 間隔長達1小時，反應太慢。改成 09:00-13:30 全交易時段每30分鐘查一次（共10次：
    # 9:00/9:30/10:00/10:30/11:00/11:30/12:00/12:30/13:00/13:30），完整涵蓋開盤到
    # 收盤。這仍然只是「現價 vs 已追蹤訊號的SL/TP」輕量比對（通常<5檔），不是重新
    # 掃描全市場找新訊號——為什麼不能在盤中重新掃描找新訊號，見 scanner.py
    # check_intraday_price_alerts() 開頭的說明。
    scheduler.add_job(job_intraday_check,   CronTrigger(hour="9-13", minute="*", day_of_week="mon-fri", timezone=TZ_TAIPEI), id="intraday_check", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_intraday_snapshot, CronTrigger(hour="9-13", minute="*/5", day_of_week="mon-fri", timezone=TZ_TAIPEI), id="intraday_snapshot", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_final_bars, CronTrigger(hour=13, minute=40, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="final_bars_1340", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_final_bars, CronTrigger(hour=14, minute=10, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="final_bars_1410", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_market_extras, CronTrigger(hour="8,17,21", minute=45, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="market_extras", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_market_extras, "date", run_date=datetime.now(TZ_TAIPEI) + __import__("datetime").timedelta(seconds=45), id="market_extras_boot", replace_existing=True)
    scheduler.add_job(job_refresh_universe_if_stale, CronTrigger(hour="15-21", minute=35, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="refresh_universe_stale", replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(job_revenue_watch, CronTrigger(day="1-15", hour="8-23", minute="*/10", timezone=TZ_TAIPEI), id="revenue_watch", replace_existing=True)
    scheduler.add_job(job_news_watch, CronTrigger(hour="8-21", minute="*/15", day_of_week="mon-fri", timezone=TZ_TAIPEI), id="news_watch", replace_existing=True)
    scheduler.add_job(job_refresh_universe, CronTrigger(hour=16, minute=0,  day_of_week="mon-fri", timezone=TZ_TAIPEI), id="refresh_universe", replace_existing=True)
    # ★ 新增：2026-09-18——見 job_pre_scan_restart() 說明，在每日掃描前主動
    # 重啟一次 worker、重置記憶體基準，預防跟 2026-09-03/2026-09-18 同一種
    # 掃描到一半被 Render 平台強制重啟、訊號整批遺失的問題。
    scheduler.add_job(job_pre_scan_restart, CronTrigger(hour=16, minute=15, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="pre_scan_restart",  replace_existing=True)
    scheduler.add_job(job_daily_scan,       CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="daily_scan",       replace_existing=True)
    scheduler.add_job(job_scan_watchdog,    CronTrigger(hour=17, minute=0,  day_of_week="mon-fri", timezone=TZ_TAIPEI), id="scan_watchdog",   replace_existing=True)
    scheduler.add_job(job_collect_inst, CronTrigger(hour="17,19", minute=40, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="collect_inst", replace_existing=True)
    scheduler.add_job(lambda: logger.debug("❤️ 心跳"), "interval", hours=1, id="heartbeat")
    scheduler.start()
    logger.info("✅ 排程器已啟動")

# ── Routes ──
@app.route("/")
def index():
    """★ 改用 send_from_directory，完全繞過 Jinja2 解析"""
    try:
        return send_from_directory(TEMPLATE_DIR, "dashboard.html")
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return (
            f"<h1 style='color:#00d47e;font-family:system-ui;padding:20px'>"
            f"🇹🇼 台股波段智慧交易系統 v{SYSTEM['version']}</h1>"
            f"<p style='padding:0 20px;color:#e6edf3;font-family:system-ui'>"
            f"✅ 系統運行中 | <a href='/api/state' style='color:#4d9eff'>/api/state</a> | "
            f"<a href='/api/diagnostics' style='color:#4d9eff'>/api/diagnostics</a><br>"
            f"錯誤：{e}</p>"
        ), 200


def _db_today_signals():
    """記憶體清單為空（剛重啟/部署）時，從資料庫還原最近一個訊號日的訊號，確保網頁與 TG 一致。"""
    try:
        import json as _j
        from state_store import store
        rows = store.get_recent_signals(limit=60, days_back=7)
        if not rows: return []
        day = (rows[0].get("generated_at") or "")[:10]
        out = []
        for r in rows:
            if (r.get("generated_at") or "")[:10] != day: continue
            try: d = _j.loads(r.get("raw_json") or "{}")
            except Exception: d = {}
            d.update({k: r.get(k) for k in ("id","ticker","code","name","direction","score","grade","entry_price","stop_loss","tp1","tp2","tp3","sl_pct","status","result","generated_at") if r.get(k) is not None})
            out.append(d)
        return out
    except Exception as e:
        logger.warning(f"_db_today_signals: {e}"); return []

def _bars_since(ticker, gen_date):
    from data_fetcher import fetch_ohlcv
    data = fetch_ohlcv(ticker, "daily") or {}
    ds = data.get("dates", [])
    idx = [i for i, d in enumerate(ds) if d and d > gen_date]
    return data, idx

def _position_view(r):
    """單筆訊號的生命週期：R倍數、離停損緩衝、持有天數、MAE/MFE、移動停損建議。"""
    entry = r.get("entry_price") or r.get("current_price") or 0
    sl = r.get("stop_loss") or 0
    buy = r.get("direction") == "buy"
    gen = (r.get("generated_at") or "")[:10]
    out = {"id": r.get("id"), "ticker": r.get("ticker"), "code": r.get("code"), "name": r.get("name"),
           "direction": r.get("direction"), "score": r.get("score"), "grade": r.get("grade"),
           "entry": entry, "stop": sl, "tp1": r.get("tp1"), "tp2": r.get("tp2"), "tp3": r.get("tp3"),
           "generated_at": r.get("generated_at"), "result": r.get("result"), "status": r.get("status"),
           "pnl_pct": r.get("pnl_pct"), "pnl_twd": r.get("pnl_twd"),
           "sector": r.get("sector"), "risk_twd": r.get("risk_twd"), "risk_pct": r.get("risk_pct"),
           "lots": r.get("suggested_lots")}
    risk = abs(entry - sl) if entry and sl else 0
    out["risk_per_share"] = round(risk, 2)
    try:
        data, idx = _bars_since(r.get("ticker"), gen)
        closes, highs, lows = data.get("closes", []), data.get("highs", []), data.get("lows", [])
        last = data.get("current_price") or (closes[-1] if closes else entry)
        out["last"] = last
        out["days_held"] = len(idx)
        if idx and entry:
            hh = max(highs[i] for i in idx); ll = min(lows[i] for i in idx)
            fav = (hh - entry) if buy else (entry - ll)
            adv = (entry - ll) if buy else (hh - entry)
            out["mfe_pct"] = round(fav / entry * 100, 2)
            out["mae_pct"] = round(-max(adv, 0) / entry * 100, 2)
            if risk:
                out["mfe_r"] = round(fav / risk, 2); out["mae_r"] = round(-max(adv, 0) / risk, 2)
        if entry and risk:
            move = (last - entry) if buy else (entry - last)
            out["r_now"] = round(move / risk, 2)
            out["pnl_now_pct"] = round(move / entry * 100, 2)
            out["cushion_pct"] = round(((last - sl) if buy else (sl - last)) / last * 100, 2) if last else None
            # 進度條：停損(0) → 進場 → TP1
            tp1 = r.get("tp1") or 0
            if tp1:
                span = (tp1 - sl) if buy else (sl - tp1)
                cur = (last - sl) if buy else (sl - last)
                out["progress"] = round(max(0, min(1, cur / span)), 3) if span else 0
                ent = (entry - sl) if buy else (sl - entry)
                out["entry_mark"] = round(max(0, min(1, ent / span)), 3) if span else 0
            # 動態停損建議：已達 +1R 抬到成本；已達 +2R 鎖 +1R
            sug, why = None, ""
            mfe_r = out.get("mfe_r", 0) or 0
            if r.get("result") == "pending":
                if mfe_r >= 2: sug, why = (entry + risk if buy else entry - risk), "已曾達 +2R，建議停損上移鎖定 +1R"
                elif mfe_r >= 1: sug, why = entry, "已曾達 +1R，建議停損上移至成本價（保本）"
            if sug: out["stop_suggest"] = round(sug, 2); out["stop_suggest_why"] = why
    except Exception as e:
        out["error"] = str(e)[:80]
    return out

def _autopsy_one(r):
    """停損解剖：為什麼這筆被洗出去。"""
    entry = r.get("entry_price") or 0
    sl = r.get("stop_loss") or 0
    buy = r.get("direction") == "buy"
    gen = (r.get("generated_at") or "")[:10]
    tags = []
    try:
        import json as _j
        raw = _j.loads(r.get("raw_json") or "{}")
    except Exception:
        raw = {}
    sl_atr = raw.get("sl_atr"); sl_pct = r.get("sl_pct") or (abs(entry - sl) / entry * 100 if entry else 0)
    if sl_atr is not None and sl_atr < 1.2: tags.append(("停損過窄", f"停損僅 {sl_atr} ATR，一般日內波動即可觸發"))
    elif sl_atr is None and sl_pct and sl_pct < 2.5: tags.append(("停損偏窄", f"停損僅 {sl_pct:.1f}%，疑似過窄"))
    ext = raw.get("ext_atr"); lvl = raw.get("chase_level")
    if lvl == "high" or (ext is not None and ext > 2.5): tags.append(("追高進場", f"進場時離 20 日線 {ext} ATR，已是強勢尾段"))
    after = None
    try:
        data, idx = _bars_since(r.get("ticker"), gen)
        ds, op, hi, lo, cl = data.get("dates", []), data.get("opens", []), data.get("highs", []), data.get("lows", []), data.get("closes", [])
        hit = None
        for i in idx:
            if (buy and lo[i] <= sl) or ((not buy) and hi[i] >= sl): hit = i; break
        if hit is not None:
            o = op[hit] if hit < len(op) else None
            if o and ((buy and o < sl) or ((not buy) and o > sl)):
                tags.append(("跳空穿越", f"開盤 {o} 已越過停損 {sl}，實際成交較差"))
            if hit == idx[0]: tags.append(("隔日即停損", "訊號隔天就被打掉，進場位置不佳"))
            later = [cl[j] for j in range(hit + 1, len(cl))]
            if later:
                rec = (max(later) if buy else min(later))
                if (buy and rec > entry) or ((not buy) and rec < entry):
                    tags.append(("洗盤後續行情", f"停損後價格回到進場價之外（{rec}），方向其實沒錯"))
            after = {"stop_date": ds[hit], "last": cl[-1] if cl else None}
    except Exception:
        pass
    if not tags: tags.append(("正常停損", "未見結構性缺陷，屬於策略的正常虧損"))
    return {"id": r.get("id"), "name": r.get("name"), "code": r.get("code"), "direction": r.get("direction"),
            "score": r.get("score"), "entry": entry, "stop": sl, "close_price": r.get("close_price"),
            "pnl_pct": r.get("pnl_pct"), "pnl_twd": r.get("pnl_twd"), "generated_at": r.get("generated_at"),
            "tags": [{"tag": t, "why": w} for t, w in tags], "after": after}

@app.route("/api/state")
def api_state():
    try:
        from scanner      import scanner
        from data_fetcher import fetch_market_overview, get_market_session
        from state_store  import store
        from risk_manager import get_system_status
        market   = fetch_market_overview()
        session  = get_market_session()
        scan_st  = scanner.get_status()
        perf     = store.get_performance_summary()
        sys_stat = get_system_status(market)
        # ★ 新增：2026-09-29——使用者反饋「資料量太少，希望包含重大訊息公告」。
        # fundamentals.fetch_material_news_risk_map() 有 6 小時記憶體快取
        # （見 fundamentals.py），這裡只是讀快取算個數，不會對外重抓，跟
        # /api/diagnostics/quality_and_news 已經在用的呼叫一樣輕量，可以放
        # 進每 30 秒被前端輪詢一次的 /api/state 不會拖慢回應。完整清單另外走
        # /api/material_news（見下方），這裡只回傳數量+狀態給側邊欄徽章用。
        try:
            from fundamentals import fetch_material_news_risk_map, material_news_fetch_ok
            _news_map = fetch_material_news_risk_map()
            news_count = sum(len(v) for v in _news_map.values())
            news_ok = material_news_fetch_ok()
        except Exception as _e:
            logger.warning(f"api_state: material_news 讀取失敗（不影響其餘欄位）: {_e}")
            news_count, news_ok = 0, False
        return jsonify({
            "version":        SYSTEM["version"],
            "system_name":    SYSTEM["name"],
            "scan_count":     scan_st["scan_count"],
            "signal_count":   scan_st["signal_count"],
            "last_scan_time": scan_st["last_scan_at"],
            "active_signals": (scan_st["signals"] or _db_today_signals())[:15],
            "market_overview":market,
            "market_session": session,
            "win_rate":       perf,
            "system_status":  sys_stat,
            "sentiment_score":market.get("sentiment_score", 50),
            "sentiment_zh":   market.get("sentiment_zh", "中性"),
            "scan_history":   store.get_scan_history(5),
            "scan_audit":     store.get_meta("last_scan_audit"),
            "material_news_count": news_count,
            "material_news_ok":    news_ok,
        })
    except Exception as e:
        logger.error(f"api_state: {e}"); return jsonify({"error": str(e)}), 500


# ── 管理後台（策略學習）：需密碼登入，見 admin_auth.py ──
import admin_auth
app.before_request(admin_auth.guard)
import members
app.register_blueprint(members.bp)
app.before_request(members.gate)   # 登入牆：登入功能啟用後，/api/* 需登入（沒設 GOOGLE_CLIENT_ID／SECRET 時整個功能關閉）
app.after_request(admin_auth.after)
import protect
protect.install(app)   # 流量湧入防護：全域限流、參數檢查、短時間快取、重端點並行上限、5xx 不外洩內容


# ════════════════════════════════════════════════
# 基礎防護：健康檢查、安全標頭、頻率限制、錯誤處理
# ★ 新增：2026-10-06（第 1 層地基）
# ════════════════════════════════════════════════
import time as _time
from collections import deque as _deque

_CSP = ("default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
_RATE = {}                    # ip -> 近 60 秒的請求時間
_RATE_LIMIT = 300             # 每個 IP 每分鐘最多 300 次 /api 請求（網頁一次載入約 20 次）
_ERR_ALERT = {}               # path -> 上次告警時間（15 分鐘內同一路徑只通知一次）


def _client_ip():
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "?")


@app.before_request
def _rate_guard():
    if not request.path.startswith("/api/"):
        return None
    now = _time.time()
    ip = _client_ip()
    q = _RATE.get(ip)
    if q is None:
        if len(_RATE) > 5000:
            _RATE.clear()
        q = _RATE[ip] = _deque()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= _RATE_LIMIT:
        r = jsonify({"error": "請求過於頻繁，請稍後再試"})
        r.status_code = 429
        r.headers["Retry-After"] = "30"
        return r
    q.append(now)
    return None


@app.after_request
def _security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    if request.path.startswith(("/api/", "/auth/")):
        h["Cache-Control"] = "no-store"          # 帶登入狀態的回應不讓瀏覽器或中間代理快取
        h.pop("Server", None)
    if "text/html" in (resp.content_type or ""):
        h.setdefault("Content-Security-Policy", _CSP)
    return resp


@app.route("/healthz")
def healthz():
    """輕量健康檢查：不碰資料庫與外部資料源，只代表程序活著。"""
    return jsonify({"ok": True, "t": int(_time.time())})


@app.route("/manifest.webmanifest")
def manifest():
    import json as _j
    icon = "data:image/svg+xml," + ("%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%230b1f3a'/%3E%3Cpath d='M6 22l6-8 5 5 9-12' stroke='%23e5484d' stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
    m = {"name": "台股波段智慧交易系統", "short_name": "台股波段", "start_url": "/", "display": "standalone",
         "background_color": "#0b1f3a", "theme_color": "#0b1f3a", "lang": "zh-TW",
         "icons": [{"src": icon, "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}
    return (_j.dumps(m, ensure_ascii=False), 200, {"Content-Type": "application/manifest+json; charset=utf-8", "Cache-Control": "public, max-age=86400"})


@app.route("/robots.txt")
def robots_txt():
    return ("User-agent: *\nDisallow: /api/\nAllow: /\n", 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=86400"})


@app.route("/favicon.ico")
def favicon_ico():
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='#0b1f3a'/>"
           "<path d='M6 22l6-8 5 5 9-12' stroke='#e5484d' stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>")
    return (svg, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=604800"})


@app.errorhandler(Exception)
def _unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    logger.exception(f"未處理的錯誤 {request.method} {request.path}: {e}")
    try:
        now = _time.time()
        if now - _ERR_ALERT.get(request.path, 0) > 900:
            _ERR_ALERT[request.path] = now
            from telegram_bot import send_alert
            send_alert(f"網站發生未預期錯誤\n{request.method} {request.path}\n{type(e).__name__}: {str(e)[:160]}", "error")
    except Exception:
        pass
    if request.path.startswith("/api/"):
        return jsonify({"error": "伺服器發生錯誤，已通知管理員"}), 500
    return "伺服器發生錯誤", 500


@app.route("/api/admin/status")
def api_admin_status():
    return admin_auth.status()


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    return admin_auth.login()


@app.route("/api/admin/ping", methods=["POST"])
def api_admin_ping():
    return admin_auth.ping()


@app.route("/api/admin/logout", methods=["POST"])
def api_admin_logout():
    return admin_auth.logout()


@app.route("/api/learning")
@admin_auth.admin_required
def api_learning():
    try:
        import learning
        return jsonify(learning.build_report())
    except Exception as e:
        logger.error(f"api_learning: {e}", exc_info=True); return jsonify({"error": str(e)}), 500


@app.route("/api/admin/shadow")
@admin_auth.admin_required
def api_admin_shadow():
    try:
        import shadow
        return jsonify(shadow.overview())
    except Exception as e:
        logger.error(f"api_admin_shadow: {e}", exc_info=True); return jsonify({"error": str(e)}), 500


@app.route("/api/admin/shadow/export.csv")
@admin_auth.admin_required
def api_admin_export():
    try:
        import shadow
        from flask import Response
        kind = request.args.get("kind") or None
        status = request.args.get("status") or None
        if kind not in (None, "candidate", "rejected", "control") or status not in (None, "pending", "closed"):
            return jsonify({"error": "參數錯誤"}), 400
        body, n = shadow.export_csv(kind, status)
        fn = f"shadow_{kind or 'all'}_{status or 'all'}_{datetime.now().strftime('%Y%m%d')}.csv"
        return Response(body, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fn}"', "X-Rows": str(n)})
    except Exception as e:
        logger.error(f"api_admin_export: {e}", exc_info=True); return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def api_health():
    try:
        import health
        return jsonify(health.run_health())
    except Exception as e:
        logger.error(f"api_health: {e}"); return jsonify({"error": str(e)}), 500


@app.route("/api/signal_check/<key>")
def api_signal_check(key):
    try:
        import health
        return jsonify(health.check_signal(key))
    except Exception as e:
        logger.error(f"api_signal_check: {e}"); return jsonify({"error": str(e)}), 500


@app.route("/api/settings")
def api_settings():
    """唯讀：目前生效的策略與風控參數，附白話說明。調整需改程式碼並部署，避免無帳號機制下被任意竄改。"""
    from config import THRESH, SHORT_SIGNAL_THRESH, SHORT_MAX_SENTIMENT, CIRCUIT_BREAKER, MAX_RISK_PER_TRADE, MAX_DAILY_RISK, ACCOUNT_BALANCE_TWD, SWING_PARAMS
    try:
        from fund_score import fund_adjustment  # noqa
    except Exception:
        pass
    g = lambda k, v, note: {"key": k, "value": v, "note": note}
    return jsonify({"groups": [
        {"title": "進場門檻", "items": [
            g("做多最低分數", THRESH.get("min_score"), "高於此分才發訊號；大盤偏弱或財報季時系統會自動再提高"),
            g("做多最低 ADX", THRESH.get("min_adx"), "趨勢強度，低於此值視為盤整不進場"),
            g("做多最低量比", THRESH.get("min_vol_ratio"), "今日量／均量"),
            g("最低風報比 (TP1)", THRESH.get("min_rr"), "到第一目標的獲利／停損風險"),
            g("做空最低分數", SHORT_SIGNAL_THRESH.get("min_score"), "做空歷史回測表現明顯較差，門檻更高且週線必須偏空"),
            g("做空允許的大盤情緒上限", SHORT_MAX_SENTIMENT, "大盤情緒分數低於此值才允許做空")]},
        {"title": "掃描池", "items": [
            g("最低收盤價", 5, "元；低於此價不掃"),
            g("最低單日成交量", THRESH.get("min_avg_volume"), "張；流動性門檻")]},
        {"title": "資金與風控", "items": [
            g("帳戶本金（試算用）", ACCOUNT_BALANCE_TWD, "TWD；建議張數依此計算，請至環境變數 ACCOUNT_BALANCE_TWD 改成你的實際本金"),
            g("單筆最大風險", f"{MAX_RISK_PER_TRADE*100:.1f}%", "每筆停損時最多虧本金的比例"),
            g("單日最大風險", f"{MAX_DAILY_RISK*100:.1f}%", "當日所有新單的風險總和上限"),
            g("同時持倉上限", 5, "達上限即暫停發新訊號"),
            g("單日最多訊號", CIRCUIT_BREAKER.get("max_daily_signals"), "避免一天塞太多單"),
            g("訊號有效天數", CIRCUIT_BREAKER.get("signal_expire_days"), "超過仍未觸及停損停利即結案")]},
        {"title": "停損停利（依市值規模）", "items": [
            g(k, f"停損 {v['sl_atr_mult']} ATR；TP1 {v['tp1_rr']}R／TP2 {v['tp2_rr']}R／TP3 {v['tp3_rr']}R", "") for k, v in SWING_PARAMS.items()]},
        {"title": "基本面加減分（做多）", "items": [
            g("基本面 ≥80", "+5", "月營收／獲利／估值綜合分"), g("≥65", "+3", ""), g("≥50", "+1", ""),
            g("≥35", "−3", ""), g("≥20", "−7", ""), g("<20", "−12", "基本面明顯轉弱的標的降低排序")]},
    ], "note": "參數為唯讀。要調整請先和我討論依據（需有足夠的結案樣本），再改程式碼部署。"})


@app.route("/api/audit")
def api_audit():
    try:
        import audit
        return jsonify(audit.get_audit(refresh=request.args.get("refresh") == "1"))
    except Exception as e:
        logger.error(f"api_audit: {e}"); return jsonify({"error": str(e)}), 500


@app.route("/api/events")
def api_events():
    from state_store import store
    return jsonify({"events": store.get_events(limit=int(request.args.get("limit", 60)))})

@app.route("/api/watchlist", methods=["GET", "POST", "DELETE"])
def api_watchlist():
    from state_store import store
    if request.method == "GET":
        return jsonify({"watchlist": store.watchlist_get()})
    j = request.get_json(silent=True) or {}
    t = (j.get("ticker") or request.args.get("ticker") or "").strip()
    if not t: return jsonify({"error": "ticker required"}), 400
    if request.method == "POST":
        store.watchlist_add(t, j.get("code", ""), j.get("name", ""), j.get("note", ""))
    else:
        store.watchlist_remove(t)
    return jsonify({"watchlist": store.watchlist_get()})

@app.route("/api/positions")
def api_positions():
    from state_store import store
    rows = store.get_recent_signals(limit=60, days_back=30)
    open_rows = [r for r in rows if r.get("result") == "pending" and r.get("status") == "active"]
    closed = [r for r in rows if r.get("result") not in ("pending", None)]
    return jsonify({"open": [_position_view(r) for r in open_rows[:12]],
                    "closed": [_position_view(r) for r in closed[:12]]})

@app.route("/api/autopsy")
def api_autopsy():
    from state_store import store
    rows = store.get_recent_signals(limit=100, days_back=60)
    sl = [r for r in rows if r.get("result") == "sl"]
    cases = [_autopsy_one(r) for r in sl[:20]]
    agg = {}
    for c in cases:
        for t in c["tags"]: agg[t["tag"]] = agg.get(t["tag"], 0) + 1
    closed = [r for r in rows if r.get("result") not in ("pending", None)]
    return jsonify({"cases": cases, "tag_counts": agg, "stop_count": len(sl), "closed_count": len(closed),
                    "total_signals": len(rows),
                    "note": "樣本偏小時（已結案 < 20 筆）統計僅供參考，不宜據此過度調參。"})


@app.route("/api/fundamentals/<code>")
def api_fundamentals(code: str):
    try:
        from fund_score import build_fundamental_profile
        return jsonify(build_fundamental_profile(code.split(".")[0]))
    except Exception as e:
        logger.error(f"api_fundamentals: {e}"); return jsonify({"error": str(e)}), 500

@app.route("/api/fundamentals_radar")
def api_fundamentals_radar():
    """營收動能榜：不看 K 線，直接用月營收＋估值在全市場找基本面轉強的公司。"""
    try:
        from fundamentals import fetch_monthly_revenue_map, fetch_valuation_map
        from fund_score import _growth, _valuation
        from state_store import store
        kind = request.args.get("kind", "growth")
        rev = fetch_monthly_revenue_map(); val = fetch_valuation_map()
        sig_codes = {r.get("code") for r in store.get_recent_signals(limit=60, days_back=7)}
        rows = []
        for code, r in rev.items():
            yoy, cum, mom, amt = r.get("yoy_pct"), r.get("cum_yoy_pct"), r.get("mom_pct"), r.get("revenue")
            if yoy is None or amt is None or amt < 100000:   # 單月營收 < 1 億（千元為單位）不列入
                continue
            # 基期太小（去年同月 < 0.8 億）或年增超過 300%，多半是一次性認列（營建案交屋、
            # 處分資產），不是可持續的成長，成長類榜單排除，避免榜首被極端值洗版。
            if "金融" in (r.get("sector_name") or ""):   # 金融保險業營收隨證券／投資損益波動，年增率不具可比性
                continue
            base = r.get("revenue_ly")
            if kind in ("growth", "accel") and ((base is not None and base < 80000) or yoy > 300):
                continue
            v = val.get(code) or {}
            pe, y = v.get("pe"), v.get("yield_pct")
            try: pe = float(pe) if pe not in (None, "", "-") else None
            except Exception: pe = None
            try: y = float(y) if y not in (None, "", "-") else None
            except Exception: y = None
            ok = False
            if kind == "growth":  ok = yoy >= 20 and (cum is None or cum >= 10)
            elif kind == "accel": ok = yoy >= 15 and cum is not None and (yoy - cum) >= 8 and (mom is None or mom > 0)
            elif kind == "value": ok = yoy > 0 and pe is not None and 0 < pe <= 15 and (y or 0) >= 4
            if not ok: continue
            g = _growth(r, []); vv = _valuation(v)
            lite = round((g["got"] + (vv["got"] if vv else 0)) / (g["max"] + (vv["max"] if vv else 0)) * 100) if g else None
            rows.append({"code": code, "name": r.get("name"), "sector": r.get("sector_name"), "period": r.get("period"),
                         "revenue_yi": round(amt / 100000, 1), "yoy": yoy, "mom": mom, "cum_yoy": cum,
                         "pe": pe, "yield": y, "score": lite, "has_signal": code in sig_codes})
        rows.sort(key=lambda x: ((x.get("score") or 0), (x.get("yoy") if kind != "value" else x.get("yield")) or 0), reverse=True)
        return jsonify({"kind": kind, "count": len(rows), "rows": rows[:60]})
    except Exception as e:
        logger.error(f"api_fundamentals_radar: {e}"); return jsonify({"error": str(e)}), 500

@app.route("/api/news/watch")
def api_news_watch():
    """我的持倉／觀察清單／今日訊號相關的最新公告（含非負面）。"""
    try:
        from fundamentals import fetch_all_news_rows
        codes = _watched_codes()
        rows = [r for r in fetch_all_news_rows() if r["code"] in codes]
        rows.sort(key=lambda r: (r.get("date", ""), r.get("time", "")), reverse=True)
        return jsonify({"codes": sorted(codes), "news": rows[:40]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _watched_codes():
    from state_store import store
    codes = {w.get("code") or (w.get("ticker") or "").split(".")[0] for w in store.watchlist_get()}
    for r in store.get_recent_signals(limit=60, days_back=10):
        if r.get("result") == "pending" and r.get("status") == "active":
            codes.add(r.get("code"))
    return {c for c in codes if c}

def job_news_watch():
    """每 15 分鐘巡一次重大訊息：持倉／觀察清單／有效訊號的公司有新公告就推播並記進通知中心。"""
    try:
        from fundamentals import fetch_all_news_rows, _cache
        from state_store import store
        _cache.pop("all_news_rows", None)
        codes = _watched_codes()
        if not codes: return
        seen = set(store.get_meta("news_seen", []) or [])
        fresh = [r for r in fetch_all_news_rows() if r["code"] in codes and f"{r['code']}|{r['date']}|{r['time']}|{r['subject'][:30]}" not in seen]
        for r in fresh[:6]:
            key = f"{r['code']}|{r['date']}|{r['time']}|{r['subject'][:30]}"
            seen.add(key)
            tag = "⚠️ 風險公告" if r["negative"] else "重大訊息"
            store.add_event("news", f"{r['name']}（{r['code']}）{tag}", r["subject"][:120], r["code"] + ".TW")
            try:
                from telegram_bot import send_alert
                send_alert(f"{r['name']}（{r['code']}）{tag}\n{r['subject'][:160]}", "warning" if r["negative"] else "info")
            except Exception as e:
                logger.warning(f"job_news_watch TG: {e}")
        if fresh:
            store.set_meta("news_seen", list(seen)[-400:])
            logger.info(f"job_news_watch: {len(fresh)} 則新公告")
    except Exception as e:
        logger.error(f"job_news_watch: {e}", exc_info=True)

def job_revenue_watch():
    """公告期（每月 1～15 日）每 10 分鐘重抓全市場月營收；持倉／觀察清單／有效訊號的公司有新一期就通知。"""
    try:
        from fundamentals import refresh_monthly_revenue, fetch_monthly_revenue_map
        from state_store import store
        changed = refresh_monthly_revenue()
        try:
            from fund_score import _persist_current_period
            _persist_current_period(fetch_monthly_revenue_map())
        except Exception as e:
            logger.warning(f"job_revenue_watch persist: {e}")
        if not changed:
            return
        logger.info(f"job_revenue_watch: {len(changed)} 檔營收有新一期")
        codes = _watched_codes()
        seen = set(store.get_meta("rev_seen", []) or [])
        for r in changed:
            if r["code"] not in codes:
                continue
            key = f"{r['code']}|{r['period']}"
            if key in seen:
                continue
            seen.add(key)
            p = str(r["period"]); ym = f"{int(p[:-2]) + 1911}/{p[-2:]}"
            yoy = r.get("yoy_pct")
            amt = f"{r['revenue'] / 100000:.1f} 億" if r.get("revenue") else "—"
            body = f"{ym} 營收 {amt}，年增 {yoy:+.1f}%" if yoy is not None else f"{ym} 營收 {amt}"
            store.add_event("revenue", f"{r['name']}（{r['code']}）公布 {ym} 月營收", body, r["code"] + ".TW")
            try:
                from telegram_bot import send_alert
                send_alert(f"{r['name']}（{r['code']}）{body}", "info")
            except Exception as e:
                logger.warning(f"job_revenue_watch TG: {e}")
        store.set_meta("rev_seen", list(seen)[-400:])
    except Exception as e:
        logger.error(f"job_revenue_watch: {e}", exc_info=True)


@app.route("/api/revenue/refresh", methods=["POST"])
def api_revenue_refresh():
    """個股研究頁的『立即更新營收』：重抓全市場月營收（限流：同一時間最多每 60 秒一次）。"""
    import time as _t
    global _rev_refresh_ts
    if _t.time() - _rev_refresh_ts < 60:
        return jsonify({"ok": False, "error": "剛更新過，請稍候 1 分鐘"}), 429
    _rev_refresh_ts = _t.time()
    try:
        from fundamentals import refresh_monthly_revenue, revenue_status
        ch = refresh_monthly_revenue()
        return jsonify({"ok": True, "changed": len(ch), "status": revenue_status()})
    except Exception as e:
        logger.error(f"api_revenue_refresh: {e}"); return jsonify({"ok": False, "error": str(e)}), 500


_rev_refresh_ts = 0.0


@app.route("/api/material_news")
def api_material_news():
    """★ 新增：2026-09-29——公開版「重大訊息公告」清單，給前端獨立面板用。
    跟 /api/diagnostics/quality_and_news 不同：這裡把 code 解析成公司名稱、
    攤平成單一時間序列 list 並依日期排序，前端不用自己再組資料結構。
    資料源見 fundamentals.py：TWSE OpenAPI t187ap04_L（上市公司重大訊息），
    只保留主旨命中負面關鍵字的筆數（見 MATERIAL_NEWS_NEGATIVE_KEYWORDS），
    僅涵蓋上市（無對應的上櫃公開資料源）。"""
    try:
        from fundamentals import fetch_material_news_risk_map, material_news_fetch_ok
        from stock_universe import build_universe
        news_map = fetch_material_news_risk_map()
        ok = material_news_fetch_ok()
        name_map = {}
        try:
            for s in build_universe():
                name_map[s.get("code", "")] = s.get("name", "")
        except Exception as _e:
            logger.warning(f"api_material_news: build_universe 讀取失敗，名稱將以代號代替: {_e}")
        items = []
        for code, entries in news_map.items():
            name = name_map.get(code, "")
            for e in entries:
                items.append({
                    "code": code,
                    "name": name or code,
                    "ticker": f"{code}.TW",
                    "date": e.get("date", ""),
                    "subject": e.get("subject", ""),
                })
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        return jsonify({
            "ok": ok,
            "count": len(items),
            "items": items[:80],
            "source": "TWSE OpenAPI t187ap04_L · 僅上市公司，近期揭露（非全歷史）",
        })
    except Exception as e:
        logger.error(f"api_material_news: {e}")
        return jsonify({"error": str(e), "ok": False, "count": 0, "items": []}), 500

@app.route("/api/signals")
def api_signals():
    from state_store import store
    limit     = int(request.args.get("limit", 20))
    direction = request.args.get("direction", "")
    sector    = request.args.get("sector", "")
    signals   = store.get_recent_signals(limit=limit * 2)
    if direction: signals = [s for s in signals if s.get("direction") == direction]
    if sector:    signals = [s for s in signals if s.get("sector") == sector]
    return jsonify({"signals": signals[:limit], "count": len(signals[:limit])})

@app.route("/api/market")
def api_market():
    try:
        from data_fetcher import fetch_market_overview
        return jsonify(fetch_market_overview())
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/universe")
def api_universe():
    try:
        from stock_universe import build_universe, get_sector_count
        universe = build_universe()
        return jsonify({"count": len(universe), "sectors": get_sector_count()})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/market/breadth")
def api_market_breadth():
    """使用者要求「市場總覽頁」，這裡是漲跌家數/漲跌停家數，資料來自
    stock_universe.get_market_breadth()（用 build_full_universe() 既有快取
    算，不額外打 API，涵蓋全部上市＋上櫃股票，不受掃描池200張門檻限制——見
    該函式 2026-09-29 修正說明）。附上 data_sources 讓使用者能核對資料來源
    與更新時間（使用者要求要能明確看到資料來源正確性）。"""
    try:
        from stock_universe import get_market_breadth, get_universe_data_meta
        result = get_market_breadth()
        result["data_sources"] = get_universe_data_meta()
        try:
            result["pool"] = _scan_pool_breadth()
        except Exception as e:
            logger.warning(f"_scan_pool_breadth: {e}")
        return jsonify(result)
    except Exception as e: return jsonify({"error": str(e)}), 500


_pool_cache = {"ts": 0, "v": None}


def _scan_pool_breadth():
    """掃描池（流動性 ≥ 門檻的上市＋上櫃）用我們自己的日線資料算的漲跌家數。
    與官方全市場統計不同：官方 OpenAPI 上市常晚一天更新，這裡兩個市場一定是同一個交易日；
    使用還原價，除權息日的漲跌幅可能與官方（用參考價）略有出入，僅供同日口徑的市場強弱參考。"""
    import time as _t
    if _pool_cache["v"] and _t.time() - _pool_cache["ts"] < 300:
        return _pool_cache["v"]
    from state_store import store
    data = store.get_daily_closes_recent(10)
    if not data:
        return None
    # 取『多數股票所在的最新日期』：盤中只有持倉／訊號股會多一根今日 K 棒，不能用 max 當全市場日期
    from collections import Counter as _C
    _cnt = _C(v[-1][0] for v in data.values() if v)
    if not _cnt:
        return None
    latest = max(d for d, c in _cnt.items() if c >= 0.5 * max(_cnt.values()))
    up = down = flat = lu = ld = n = stale = 0
    for t, rows in data.items():
        if len(rows) < 2: continue
        if rows[-1][0] != latest:
            stale += 1; continue
        p, c = rows[-2][1], rows[-1][1]
        if not p or p <= 0: continue
        chg = (c / p - 1) * 100; n += 1
        if chg > 0.0001: up += 1
        elif chg < -0.0001: down += 1
        else: flat += 1
        if chg >= 9.5: lu += 1
        elif chg <= -9.5: ld += 1
    v = {"date": latest[:10], "n": n, "advancers": up, "decliners": down, "unchanged": flat,
         "limit_up": lu, "limit_down": ld, "stale": stale,
         "adv_ratio": round(up / n * 100, 1) if n else None}
    _pool_cache.update(ts=_t.time(), v=v)
    return v


@app.route("/api/market/sectors")
def api_market_sectors():
    """產業當日漲跌排行，同樣用 build_full_universe() 既有快取算，見
    stock_universe.get_sector_performance()。"""
    try:
        from stock_universe import get_sector_performance, get_universe_data_meta
        return jsonify({"sectors": get_sector_performance(), "data_sources": get_universe_data_meta()})
    except Exception as e: return jsonify({"error": str(e)}), 500


def _rk_row(s):
    c, v = s.get("close") or 0, s.get("volume_lots") or 0
    return {"code": s.get("code"), "name": s.get("name"), "sector": s.get("sector") or "", "market": s.get("market"),
            "close": c, "change_pct": s.get("change_pct"), "volume_lots": int(v),
            "value_yi": round(c * v * 1000 / 1e8, 2), "is_etf": bool(s.get("is_etf"))}


@app.route("/api/market/rankings")
def api_market_rankings():
    """排行榜（漲幅／跌幅／成交金額／成交量，上市＋上櫃分開）＋產業熱度（每產業平均漲跌、領漲股、成交金額占比）。
    全部由 build_full_universe() 既有快取算出，不額外打外部 API。"""
    try:
        from stock_universe import build_live_universe, get_universe_data_meta
        uni = [s for s in build_live_universe() if s.get("close")]
        out = {}
        for mk in ("TSE", "OTC"):
            L = [s for s in uni if s.get("market") == mk]
            liquid = [s for s in L if (s.get("volume_lots") or 0) >= 500 and s.get("change_pct") is not None and not s.get("is_etf")]
            val = lambda s: (s.get("close") or 0) * (s.get("volume_lots") or 0)
            out[mk] = {
                "gain": [_rk_row(s) for s in sorted(liquid, key=lambda s: -s["change_pct"])[:20]],
                "loss": [_rk_row(s) for s in sorted(liquid, key=lambda s: s["change_pct"])[:20]],
                "value": [_rk_row(s) for s in sorted(L, key=lambda s: -val(s))[:20]],
                "volume": [_rk_row(s) for s in sorted(L, key=lambda s: -(s.get("volume_lots") or 0))[:20]],
                "n": len(L),
            }
        by = {}
        for s in uni:
            if s.get("change_pct") is None or s.get("is_etf"):
                continue
            by.setdefault(s.get("sector") or "其他", []).append(s)
        tot = sum((s.get("close") or 0) * (s.get("volume_lots") or 0) for L in by.values() for s in L) or 1
        heat = []
        for sec, L in by.items():
            if len(L) < 5 or sec == "其他":
                continue
            liq = [s for s in L if (s.get("volume_lots") or 0) >= 500] or L
            lead = max(liq, key=lambda s: s["change_pct"])
            heat.append({"sector": sec, "n": len(L), "avg": round(sum(s["change_pct"] for s in L) / len(L), 2),
                         "up": sum(1 for s in L if s["change_pct"] > 0), "down": sum(1 for s in L if s["change_pct"] < 0),
                         "share": round(sum((s.get("close") or 0) * (s.get("volume_lots") or 0) for s in L) / tot * 100, 1),
                         "leader": {"code": lead["code"], "name": lead["name"], "chg": lead["change_pct"]}})
        heat.sort(key=lambda x: -x["avg"])
        dates = [s.get("quote_date") for s in uni if s.get("quote_date")]
        return jsonify({"date": max(dates) if dates else None, "markets": out, "heat": heat,
                        "data_sources": get_universe_data_meta()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market/inst-rank")
def api_market_inst_rank():
    """三大法人買賣超排行（T86，僅上市）。資料量大，與主排行分開載入。"""
    try:
        from data_fetcher import fetch_institutional_flow, inst_status
        from stock_universe import build_live_universe
        flow = fetch_institutional_flow() or {}
        meta = inst_status() or {}
        px = {s.get("code"): s for s in build_live_universe()}
        rows = []
        for code, v in flow.items():
            u = px.get(code) or {}
            if u.get("is_etf") or not u.get("close"):
                continue
            rows.append({"code": code, "name": v.get("name") or u.get("name"), "sector": u.get("sector") or "",
                         "close": u.get("close"), "change_pct": u.get("change_pct"),
                         "foreign": v.get("foreign_net", 0), "trust": v.get("trust_net", 0), "total": v.get("total_net", 0)})
        top = lambda k, rev: sorted(rows, key=lambda r: r[k], reverse=rev)[:20]
        return jsonify({"date": meta.get("date"), "n": len(rows),
                        "foreign_buy": top("foreign", True), "foreign_sell": top("foreign", False),
                        "trust_buy": top("trust", True), "trust_sell": top("trust", False)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/market/inst-streak")
def api_market_inst_streak():
    """外資／投信連續買超、賣超排行（依累積的多日資料計算；僅上市、不含 ETF）。"""
    import time as _t
    _c = _INST_STREAK_CACHE
    if _c["v"] is not None and _t.time() - _c["t"] < 120:
        return jsonify(_c["v"])
    with _c["lock"]:
        if _c["v"] is not None and _t.time() - _c["t"] < 120:
            return jsonify(_c["v"])
        resp = _inst_streak_compute()
        if "error" in resp:
            return jsonify(resp), 500
        _c["v"] = resp; _c["t"] = _t.time()
        return jsonify(resp)


import threading as _th
_INST_STREAK_CACHE = {"t": 0, "v": None, "lock": _th.Lock()}


def _inst_streak_compute():
    try:
        from state_store import store
        from stock_universe import build_full_universe
        dates = store.get_inst_dates(limit=30)            # 新到舊
        if not dates:
            return {"days": 0, "dates": [], "msg": "尚未累積資料"}
        rows = store.get_inst_since(dates[-1])
        by = {}
        for r in rows:
            by.setdefault(r["code"], {})[r["trade_date"]] = r
        px = {s.get("code"): s for s in build_full_universe()}
        out = []
        for code, dd in by.items():
            u = px.get(code) or {}
            if u.get("is_etf") or not u.get("close"):
                continue
            rec = {"code": code, "name": u.get("name") or next(iter(dd.values())).get("name"),
                   "sector": u.get("sector") or "", "close": u.get("close"), "change_pct": u.get("change_pct")}
            for key, tag in (("foreign_net", "f"), ("trust_net", "t")):
                seq = [(dd.get(d) or {}).get(key, 0) or 0 for d in dates]      # 新到舊，缺日視為 0
                sign = 1 if seq[0] > 0 else -1 if seq[0] < 0 else 0
                n = 0; cum = 0
                if sign:
                    for v in seq:
                        if (v > 0) == (sign > 0) and v != 0:
                            n += 1; cum += v
                        else:
                            break
                rec[tag + "_streak"] = n * sign
                rec[tag + "_cum"] = cum
                rec[tag + "_5d"] = sum(seq[:5])
                rec[tag + "_today"] = seq[0]
            out.append(rec)
        def top(tag, sign):
            c = [r for r in out if r[tag + "_streak"] * sign >= 2]
            c.sort(key=lambda r: (abs(r[tag + "_streak"]), abs(r[tag + "_cum"])), reverse=True)
            return c[:30]
        return {"days": len(dates), "dates": dates[:5], "latest": dates[0],
                "foreign_buy": top("f", 1), "foreign_sell": top("f", -1),
                "trust_buy": top("t", 1), "trust_sell": top("t", -1)}
    except Exception as e:
        return {"error": str(e)}


@app.route("/api/quote/<code>")
def api_quote(code):
    """單一檔盤中即時報價（官方：證交所 MIS；備援：富果）。抓不到就明講抓不到，不拿舊資料充數。"""
    try:
        import realtime
        from stock_universe import build_full_universe
        c = code.split(".")[0].upper()
        mk = None
        for s in build_full_universe():
            if s.get("code") == c:
                mk = s.get("market"); break
        q = realtime.get_quote(c, mk)
        if not q:
            return jsonify({"ok": False, "code": c, "error": "即時報價暫時抓不到（證交所 MIS 與備援都失敗）",
                            "status": realtime.status()}), 200
        q["ok"] = True
        q["market_open"] = realtime.market_open()
        return jsonify(q)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/public/pulse")
def api_public_pulse():
    """未登入首頁用：只公開加權指數的最近快照與當日走勢（唯讀、讀快取、不打外部來源）。其餘資料都要登入。"""
    try:
        import realtime, time as _t
        from state_store import store
        snap = store.get_meta("intraday_snapshot") or {}
        idx = snap.get("indices") or {}
        if not any((v or {}).get("price") for v in idx.values()):
            idx = store.get_meta("index_last") or {}            # 即時來源失敗時退回最後一次成功的指數
        name = "TAIEX" if "TAIEX" in idx else next(iter(idx), None)
        v = idx.get(name) or {}
        cur = store.get_meta("index_intraday") or {}
        today = realtime.now_tpe().strftime("%Y-%m-%d")
        series = (cur.get("series") or {}).get(name, []) if cur.get("date") == today else []
        prev = v.get("prev_close")
        price = v.get("price")
        chg = round(price - prev, 2) if price is not None and prev else None
        pct = round(chg / prev * 100, 2) if chg is not None and prev else None
        return jsonify({"ok": bool(v), "name": name, "price": price, "prev": prev, "chg": chg, "pct": pct,
                        "date": v.get("date"), "series": series[-80:], "market_open": realtime.market_open()})
    except Exception:
        return jsonify({"ok": False})


@app.route("/api/intraday/overview")
def api_intraday_overview():
    """盤中市場總覽：指數、漲跌家數、漲跌停家數，皆來自最近一次官方即時快照；超過 10 分鐘就標示過期。"""
    try:
        import realtime, time as _t
        from state_store import store
        snap = store.get_meta("intraday_snapshot") or {}
        at = snap.get("at")
        age = round(_t.time() - at) if at else None
        today = realtime.now_tpe().strftime("%Y-%m-%d")
        idx = snap.get("indices") or {}
        from stock_universe import snap_is_final
        final = bool(at and snap_is_final(snap, _t.time()))
        fresh = bool(at and age is not None and age <= 600 and any((v or {}).get("date") == today for v in idx.values()))
        if final and any((v or {}).get("date") == today for v in idx.values()):
            fresh = True
        return jsonify({"ok": bool(snap), "fresh": fresh, "final": final, "age_secs": age, "market_open": realtime.market_open(),
                        "indices": idx, "breadth": snap.get("breadth"), "n_ok": snap.get("n_ok"),
                        "n_req": snap.get("n_req"), "completeness": snap.get("completeness"),
                        "quality": snap.get("quality")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/intraday/series")
def api_intraday_series():
    """當日指數走勢（每 5 分鐘一點，從開盤累積；隔日重置）。"""
    try:
        import realtime
        from state_store import store
        cur = store.get_meta("index_intraday") or {}
        today = realtime.now_tpe().strftime("%Y-%m-%d")
        if cur.get("date") != today:
            return jsonify({"ok": True, "date": today, "series": {}, "prev": {}})
        return jsonify({"ok": True, "date": today, "series": cur.get("series", {}), "prev": cur.get("prev", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/taifex")
def api_taifex():
    """期交所籌碼：外資／投信／自營商期貨未平倉（大台等效）、臺指選擇權、Put/Call 比、大額交易人。"""
    try:
        import taifex
        from state_store import store
        return jsonify(taifex.summary(store))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/db_stats")
def api_db_stats():
    """資料庫用量：各表筆數與大小、meta 各鍵大小、K 線覆蓋檔數與日期範圍（只有計數，不含內容）。"""
    from state_store import store, USE_PG
    out = {"engine": "postgres" if USE_PG else "sqlite", "tables": [], "meta_top": []}
    try:
        with store._conn() as conn:
            if USE_PG:
                out["db_bytes"] = conn.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
                names = [r["t"] for r in conn.execute("SELECT relname AS t FROM pg_stat_user_tables").fetchall()]
            else:
                names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in names:
                try:
                    n = conn.execute(f'SELECT COUNT(*) AS n FROM "{t}"').fetchone()["n"]
                    b = conn.execute("SELECT pg_total_relation_size(?::regclass) AS b", (t,)).fetchone()["b"] if USE_PG else None
                    out["tables"].append({"table": t, "rows": n, "bytes": b})
                except Exception as e:
                    out["tables"].append({"table": t, "error": str(e)})
            out["tables"].sort(key=lambda x: -(x.get("bytes") or x.get("rows") or 0))
            for r in conn.execute("SELECT key, LENGTH(value) AS b FROM meta ORDER BY 2 DESC LIMIT 12").fetchall():
                out["meta_top"].append({"key": r["key"], "bytes": r["b"]})
            try:
                r = conn.execute("SELECT COUNT(DISTINCT ticker) AS n, MIN(bar_date) AS a, MAX(bar_date) AS z FROM ohlcv_bars WHERE tf_key='daily'").fetchone()
                out["ohlcv_daily"] = {"tickers": r["n"], "from": r["a"], "to": r["z"]}
            except Exception:
                pass
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out)


@app.route("/api/market_extras")
def api_market_extras():
    """交易限制旗標與借券／融券歷史的狀態（資料筆數、錯誤、最近日期）。"""
    import market_extras
    from state_store import store
    f = market_extras.get_flags()
    h = store.get_meta("short_hist") or {}
    return jsonify({"status": market_extras.status(), "date": f.get("date"), "errors": f.get("errors"),
                    "halt": f.get("halt"), "altered": f.get("altered"), "margin_stop": len(f.get("margin_stop") or {}),
                    "short_hist_days": sorted(h.keys())})


@app.route("/api/quote_status")
def api_quote_status():
    """即時報價來源健康狀態＋最近一次盤中快照的時間與檔數。"""
    try:
        import realtime
        from state_store import store
        snap = store.get_meta("intraday_snapshot") or {}
        _probe = request.args.get("probe")
        if _probe:
            return jsonify({"probe": realtime.probe(_probe.split(".")[0].upper(), request.args.get("market"))})
        _qs = (snap.get("quotes") or {})
        import data_fetcher as _df
        return jsonify({"source": realtime.status(), "market_open": realtime.market_open(),
                        "official_bars": _df._obar_stats,
                        "snapshot_priced": sum(1 for v in _qs.values() if v and v[0]),
                        "snapshot_at": snap.get("at"), "snapshot_n": snap.get("n_ok"),
                        "snapshot_secs": snap.get("secs")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analysis/foreign-flow")
def api_analysis_foreign_flow():
    """外資買賣超門檻檢驗（研究用）。第一次呼叫會在背景計算（約 5～8 分鐘），之後讀取快取結果；加 ?rerun=1 重算。"""
    try:
        import analysis_foreign as af
        st = af.status()
        if request.args.get("rerun") == "1" or (not st["result"] and not st["state"]["running"]):
            af.start(); st = af.status()
        return jsonify(st)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calendar")
def api_calendar():
    """近期除權息＋處置／注意股，並標出與目前持倉／觀察清單重疊者。"""
    try:
        from calendar_events import get_ex_dividend, get_listed_watch
        from state_store import store
        ex = get_ex_dividend() or {}
        wt = get_listed_watch() or {}
        mine = set()
        try:
            for p in store.get_recent_signals(limit=60, days_back=30):
                if p.get("result") == "pending" and p.get("status") == "active":
                    mine.add(str(p.get("ticker", "")).split(".")[0])
        except Exception:
            pass
        for x in ex.get("items", []):
            x["mine"] = x["code"] in mine
        for k in ("disposal", "attention"):
            for x in wt.get(k, []):
                x["mine"] = x["code"] in mine
        return jsonify({"ex": ex, "watch": wt})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/screener")
def api_screener():
    """市場總覽頁的股票篩選器。只支援 stock_universe.py 既有欄位（代號/名稱/
    產業/市值分類/價格/漲跌幅/成交量），不支援 RSI/MACD/均線這類技術指標篩選
    ——對全市場 1000+ 檔即時算技術指標成本太高，現有掃描架構只對候選訊號的
    幾十檔算（見 scanner.py），這裡先不做，避免每次篩選都變成一次重量級全
    市場運算。
    ★ 修正：2026-09-29——改用不設門檻的 screen_universe()（見該函式說明），
    查詢範圍涵蓋全部上市＋上櫃股票，不再受掃描池200張成交量門檻限制。每筆
    結果額外附上 in_scan_universe（是否會被每日訊號掃描納入候選）、
    is_disposal_or_attention（是否為目前列管的處置/注意股）兩個旗標，讓使用
    者篩到成交量很低或列管中的股票時，能清楚知道這點、不會誤以為是一般股票。"""
    try:
        from stock_universe import screen_universe, get_universe_data_meta
        from scanner import scanner
        filters = {
            "q": request.args.get("q"),
            "sector": request.args.get("sector"),
            "size_cat": request.args.get("size_cat"),
        }
        for k in ("min_price","max_price","min_chg_pct","max_chg_pct","min_volume_lots"):
            v = request.args.get(k)
            if v not in (None, ""):
                try: filters[k] = float(v)
                except ValueError: pass
        limit = int(request.args.get("limit", 100))
        results = screen_universe(filters)
        # 標記目前有沒有對應的有效訊號，讓篩選結果可以直接跳去個股研究頁看訊號原因
        signal_tickers = {s.get("ticker") for s in scanner.get_status().get("signals", [])}
        for s in results:
            s["has_signal"] = s.get("ticker") in signal_tickers
        return jsonify({"count": len(results), "total_matched": len(results), "items": results[:limit],
                         "data_sources": get_universe_data_meta()})
    except Exception as e: return jsonify({"error": str(e)}), 500

_long_hist_cache = {}
from concurrent.futures import ThreadPoolExecutor as _TPE
_INST_POOL = _TPE(max_workers=4)


def _long_history(t):
    """K 線圖用的長歷史（約 5 年日線，還原價）。與掃描共用的 fetch_ohlcv 分開，避免影響掃描快取；
    30 分鐘快取。失敗回 None（前端退回使用一年資料）。"""
    import time as _t
    c = _long_hist_cache.get(t)
    if c and _t.time() - c[0] < 1800:
        return c[1]
    try:
        import yfinance as yf
        h = yf.Ticker(t).history(period="5y", interval="1d", auto_adjust=True)
        if h is None or h.empty:
            return None
        h = h.dropna(subset=["Open", "High", "Low", "Close"])
        d = {"dates": [i.strftime("%Y-%m-%d") for i in h.index],
             "opens": [round(float(x), 2) for x in h["Open"]], "highs": [round(float(x), 2) for x in h["High"]],
             "lows": [round(float(x), 2) for x in h["Low"]], "closes": [round(float(x), 2) for x in h["Close"]],
             "volumes": [int(float(x) // 1000) for x in h["Volume"].fillna(0)]}
        bad = sum(1 for i in range(len(d["dates"])) if d["highs"][i] < d["lows"][i]
                  or not (d["lows"][i] - 1e-6 <= d["closes"][i] <= d["highs"][i] + 1e-6))
        d["quality"] = {"bars": len(d["dates"]), "bad_ohlc": bad, "zero_volume": sum(1 for v in d["volumes"] if v == 0),
                        "source": "Yahoo Finance 日線（還原價，已含除權息調整）"}
        _long_hist_cache[t] = (_t.time(), d)
        return d
    except Exception as e:
        logger.warning(f"_long_history {t}: {e}")
        return None


@app.route("/api/instruments/<ticker>")
def api_instrument(ticker: str):
    """★ 新增：2026-09-29——個股研究頁的主要資料來源，把已經存在、但分散在
    各個模組的資料聚合成一份回應：基本資料（stock_universe）、日線行情+
    技術指標（data_fetcher.fetch_ohlcv + indicators.calc_all_indicators，
    跟 scanner.py 產生訊號用的是同一套計算）、基本面（get_profitability_
    quality/估值/月營收）、重大訊息公告、目前有效訊號、近期歷史訊號。任一
    區塊失敗只影響那個區塊（優雅降級，其餘照常回傳），不會因為某個資料源
    掛掉就整頁失敗。
    ★ 修正：2026-09-29——使用者反映「查詢限制在200張(成交量門檻)以內」——
    原本用 get_stock_info()（查 build_universe() 的掃描池，受 config.py
    THRESH['min_avg_volume'] 200張門檻限制）取基本資料，代表成交量沒有到
    200張的股票（尤其是大多數上櫃小型股）完全查不到。改用不設門檻的
    get_stock_info_any()（查 build_full_universe()，涵蓋全部上市＋上櫃
    股票），查詢範圍不再受掃描池門檻限制。同時使用者要求要能清楚看到每一
    塊資料的來源/正確性，所以加了 data_sources 區塊、in_scan_universe／
    is_disposal_or_attention 旗標。"""
    raw = ticker.upper()
    had_suffix = "." in raw
    t = raw if had_suffix else raw + ".TW"
    code = t.split(".")[0]
    out = {"ticker": t, "code": code}
    try:
        from stock_universe import get_stock_info_any
        info = get_stock_info_any(t)
        # 使用者只輸入代號、不帶 .TW/.TWO 後綴時，這裡預設先猜 .TW（上市），但
        # 很多股票其實是上櫃（.TWO），猜錯會導致整頁查無資料。build_full_universe()
        # 同時涵蓋上市+上櫃全部股票，所以猜 .TW 沒找到、使用者原本又沒指定後綴時，
        # 改猜 .TWO 再試一次；兩次都沒有才真的代表這檔不存在於 TWSE/TPEX 公開清單
        # （例如下市、代號輸入錯誤）。
        if info is None and not had_suffix:
            t2 = code + ".TWO"
            info2 = get_stock_info_any(t2)
            if info2 is not None:
                t = t2; info = info2
                out["ticker"] = t
        out["info"] = info
    except Exception as e:
        out["info"] = None; out["info_error"] = str(e)
    try:
        import market_extras
        from state_store import store as _st2
        out["flags"] = market_extras.stock_flags(code, (out.get("info") or {}).get("volume_lots"), _st2)
    except Exception as e:
        out["flags"] = {"flags": [], "error": str(e)}
    try:
        from data_fetcher import fetch_ohlcv
        # 5 年長歷史與一年日線互不相依，原本串行（冷快取時合計近 20 秒），改成同時抓。
        _fut_long = _INST_POOL.submit(_long_history, t)
        ohlcv = fetch_ohlcv(t, "daily")
        out["ohlcv"] = ohlcv
        if ohlcv:
            from indicators import calc_all_indicators
            out["indicators"] = calc_all_indicators(ohlcv)
        else:
            out["indicators"] = None
        try:
            out["ohlcv_long"] = _fut_long.result(timeout=45)
        except Exception as _e:
            logger.warning(f"ohlcv_long {t}: {_e}"); out["ohlcv_long"] = None
        try:
            from state_store import store as _st
            ov = _st.get_official_volumes(code)
            n_ov = 0
            for _o in (out.get("ohlcv"), out.get("ohlcv_long")):
                if not _o or not _o.get("dates"):
                    continue
                vols = list(_o.get("volumes") or [])
                flags = [0] * len(vols)
                for i, d_ in enumerate(_o["dates"]):
                    if d_ in ov and i < len(vols):
                        vols[i] = ov[d_]; flags[i] = 1
                _o["volumes"] = vols; _o["vol_official"] = flags
            n_ov = len(ov)
            out["vol_official_days"] = n_ov
            _kick_backfill(code, t, n_ov)
        except Exception as _e:
            out["vol_official_days"] = 0
    except Exception as e:
        out["ohlcv"] = None; out["indicators"] = None; out["ohlcv_error"] = str(e)
    try:
        from fundamentals import get_profitability_quality, fetch_valuation_map, fetch_monthly_revenue_map
        out["profitability_quality"] = get_profitability_quality(code)
        out["valuation"] = fetch_valuation_map().get(code)
        out["monthly_revenue"] = fetch_monthly_revenue_map().get(code)
        try:
            from fundamentals import revenue_status
            out["revenue_status"] = revenue_status(code)
        except Exception as _e:
            out["revenue_status"] = {"error": str(_e)}
        try:
            from fund_score import build_fundamental_profile
            out["fundamental_profile"] = build_fundamental_profile(code)
        except Exception as _e:
            out["fundamental_profile"] = {"error": str(_e)}
    except Exception as e:
        out["fundamentals_error"] = str(e)
    try:
        from fundamentals import fetch_material_news_risk_map
        out["material_news"] = fetch_material_news_risk_map().get(code, [])
    except Exception as e:
        out["material_news"] = []; out["material_news_error"] = str(e)
    try:
        from scanner import scanner
        live = [s for s in scanner.get_status().get("signals", []) if s.get("ticker") == t]
        out["active_signal"] = live[0] if live else None
    except Exception as e:
        out["active_signal"] = None; out["active_signal_error"] = str(e)
    try:
        from state_store import store
        history = [s for s in store.get_recent_signals(limit=300, days_back=180) if s.get("ticker") == t]
        out["signal_history"] = history[:20]
    except Exception as e:
        out["signal_history"] = []; out["signal_history_error"] = str(e)
    # ★ 新增：2026-09-29——使用者要求「保證資料來源的正確性以及準確度」，把每
    # 一塊資料實際的來源、更新頻率、已知限制明白列出來，不要讓使用者自己猜。
    try:
        from stock_universe import get_universe_data_meta
        meta = get_universe_data_meta()
        out["data_sources"] = {
            "quote_and_sector": {
                "quote_source": meta["quote_source"],
                "sector_source": meta["sector_source"],
                "sector_data_ok": meta["sector_data_ok"],
                "fetched_at": meta["fetched_at"],
                "update_freq": "每日一次（收盤後），非即時盤中報價",
                "scan_universe_threshold": meta["scan_universe_threshold"],
                # ★ 修正：2026-09-29——使用者回報「個股研究的資料有錯誤（上漲/
                # 下跌）」。根因：這裡原本一律顯示 meta["quote_trading_date"]——
                # 這是「優先取 TWSE 上市日期」的全市場代表日期，但 TWSE（上市）
                # 跟 TPEX（上櫃）兩邊官方資料目前不是同一天更新（TWSE 卡在假期
                # 前、TPEX 已經是今天），查詢一檔上櫃(.TWO)股票時，這檔股票自己
                # 的 info.quote_date 其實是正確、最新的日期，但頁面卻顯示了
                # TWSE 那個（對這檔股票而言是錯的）全市場日期，讓使用者以為看到
                # 的是舊資料，或反過來以為新資料是舊的——兩種情況都會被誤會成
                # 「資料算錯了」。修正：優先使用這檔股票自己的 info.quote_date
                # （來源同一份 stock_universe 資料，只是沒有被混用到別的交易所日
                # 期），查無 info 時才退回全市場代表日期。
                "quote_trading_date": ((out.get("info") or {}).get("quote_date")) or meta.get("quote_trading_date"),
                # 上市/上櫃兩邊「全市場」各自最新的資料日期＋是否不一致，供前端在
                # 兩邊確實不同天時額外示警（例如查上市股卻想順便知道上櫃那邊落後）。
                "tse_quote_date": meta.get("tse_quote_date"),
                "otc_quote_date": meta.get("otc_quote_date"),
                "dates_mismatch": meta.get("dates_mismatch", False),
            },
            "technical_indicators": {
                "source": "yfinance 日線 OHLCV，計算方式與每日訊號掃描（scanner.py）完全相同",
                "note": "K線根數不足（EMA長期均線需120根以上）時 indicators.valid=false，各子指標亦可能個別缺資料",
            },
            "fundamentals": {
                "valuation_source": (out.get("valuation") or {}).get("source", "twse_openapi／tpex_openapi（本益比/殖利率/股價淨值比）"),
                "monthly_revenue_source": (out.get("monthly_revenue") or {}).get("source", "twse_openapi／tpex_openapi（月營收）"),
                "note": "估值與營收資料可能落後（非即時），實際期間以回傳的 period 欄位為準",
            },
            "material_news": {"source": "TWSE OpenAPI t187ap04_L（重大訊息公告，僅涵蓋上市、非全歷史；上櫃無對應公開資料源）"},
        }
    except Exception as e:
        out["data_sources_error"] = str(e)
    return jsonify(out)

@app.route("/api/scan/force", methods=["POST"])
def api_force_scan():
    # ★ 修正：2026-09-03——scanner.run_daily_scan() 現在有掃描鎖，重複觸發時
    # 會直接跳過而不是疊加執行；這裡先檢查狀態，讓使用者連點按鈕時看到清楚的
    # 「掃描進行中」訊息，而不是誤以為又開始了一次新的掃描。
    try:
        from scanner import scanner
        if scanner.is_scanning:
            return jsonify({"status": "already_scanning"})
    except Exception:
        pass
    threading.Thread(target=job_daily_scan, daemon=True).start()
    return jsonify({"status": "scanning_started"})

@app.route("/api/performance")
def api_performance():
    from state_store import store
    return jsonify(store.get_performance_summary())

@app.route("/api/backtest/<ticker>")
def api_backtest(ticker: str):
    try:
        from backtester import backtest_symbol_tw
        t = ticker.upper()
        if ".TW" not in t: t += ".TW"
        return jsonify(backtest_symbol_tw(t))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/backtest/<ticker>/walkforward")
def api_walkforward(ticker: str):
    try:
        from backtester import walk_forward_backtest_tw
        t = ticker.upper()
        if ".TW" not in t: t += ".TW"
        return jsonify(walk_forward_backtest_tw(t))
    except Exception as e: return jsonify({"error": str(e)}), 500

# ★ 新增：2026-09-19——使用者要求對整套策略（含做多/做空兩個方向）做「非常
# 詳細的檢測並測驗」，不能只靠邏輯推演回答「可不可行」，需要真的拿歷史資料跑
# 一次回測。backtester.run_full_backtest_tw() 原本就有、但從沒接過路由，直接
# 同步呼叫的話（TW50共50檔，每檔序列fetch+計算+1秒間隔）跑完可能要好幾分鐘，
# 會超過 gunicorn 120秒逾時被砍斷。這裡比照 job_daily_scan 的背景執行緒模式：
# /run 立刻回傳、實際工作丟到背景執行緒跑，進度跟最終結果都寫進 state_store
# 的 meta（get_meta/set_meta 本來就有，last_scan_at 也是這樣做持久化的），
# /result 隨時可以查目前進度或拿到已完成的結果，不用整個request卡住等。
_full_backtest_running = threading.Event()

# ★ 修正：2026-09-29（跨AI覆核，ChatGPT指出「回測股票池 vs 即時掃描池不一致」，
# 經驗證確實屬實）——原本這裡永遠只回測寫死的TW50這50檔，但scanner.py即時
# 掃描的是全市場1000+檔，回測數字跟即時系統實際在做的事情根本不是同一件事。
# 現在加一個 universe 參數：預設仍是 "tw50"（維持原本速度快、適合快速迭代
# 驗證單一因子改動的用途，不改變既有呼叫端行為），但可以傳 universe=full
# 改成回測 stock_universe.build_universe() 回傳的「今天」完整即時掃描池
# （1000+檔），跟即時系統用同一份股票池，解決「兩邊池子不一樣」這個問題。
# 誠實的限制：build_universe()用的是「現在」的股票清單套用到過去的回測期間，
# 過去曾經下市/被剔除的股票依然不會出現在裡面（存活者偏差本身沒有完全解決，
# 需要歷史每日真實成分股清單這種目前系統沒有的資料源才能徹底解決），這裡回
# 傳的 result 會明確標註 universe_mode 跟這個限制，不能讓使用者誤以為
# universe=full 就是「完全沒有偏差」的回測。
def _run_full_backtest_bg(min_score, universe_mode="tw50"):
    from state_store import store
    if _full_backtest_running.is_set():
        return
    _full_backtest_running.set()
    try:
        from backtester import run_full_backtest_tw
        if universe_mode == "full":
            from stock_universe import build_universe
            tickers = [s["ticker"] for s in build_universe()]
        else:
            tickers = None  # None → run_full_backtest_tw 內部退回 get_tw50_components()
        store.set_meta("full_backtest_progress", {"status": "running", "done": 0, "total": 0, "ticker": "", "universe_mode": universe_mode})
        def _cb(done, total, ticker):
            store.set_meta("full_backtest_progress", {"status": "running", "done": done, "total": total, "ticker": ticker, "universe_mode": universe_mode})
        result = run_full_backtest_tw(tickers=tickers, min_score=min_score, progress_cb=_cb)
        result["universe_mode"] = universe_mode
        result["universe_note"] = ("回測範圍：今天的完整即時掃描池（約" + str(len(tickers or [])) + "檔，跟scanner.py即時系統同一份股票池）。"
                                    "但仍套用「現在」的清單到過去日期，曾經下市/被剔除的股票不會出現，存活者偏差未完全消除。"
                                    if universe_mode == "full" else
                                    "回測範圍：寫死的台灣50成分股（50檔），跟即時系統實際掃描的1000+檔股票池不是同一份，僅供快速驗證策略邏輯用，不代表即時系統的真實績效分佈。")
        store.set_meta("full_backtest_result", result)
        store.set_meta("full_backtest_progress", {"status": "done", "done": result.get("total", 0), "total": result.get("total", 0), "ticker": "", "universe_mode": universe_mode})
        logger.info(f"[BT] 批量回測完成（universe={universe_mode}），共 {result.get('total',0)} 檔有效結果")
    except Exception as e:
        logger.error(f"_run_full_backtest_bg: {e}", exc_info=True)
        store.set_meta("full_backtest_progress", {"status": "error", "error": str(e)})
    finally:
        _full_backtest_running.clear()

@app.route("/api/backtest/full/run", methods=["POST"])
def api_backtest_full_run():
    if _full_backtest_running.is_set():
        return jsonify({"status": "already_running"})
    min_score = request.args.get("min_score", default=65.0, type=float)
    universe_mode = request.args.get("universe", default="tw50", type=str)
    if universe_mode not in ("tw50", "full"):
        return jsonify({"error": "universe 參數必須是 tw50 或 full"}), 400
    threading.Thread(target=_run_full_backtest_bg, args=(min_score, universe_mode), daemon=True).start()
    note = "TW50成分股（50檔），背景執行" if universe_mode == "tw50" else "完整即時掃描池（約1000+檔，跟scanner.py同一份股票池），背景執行，檔數較多會明顯較慢"
    return jsonify({"status": "started", "min_score": min_score, "universe": universe_mode,
                     "note": f"{note}，用 /api/backtest/full/result 查進度"})

@app.route("/api/backtest/full/result")
def api_backtest_full_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("full_backtest_progress", {"status": "never_run"}),
        "result": store.get_meta("full_backtest_result", None),
    })

# ★ 新增：2026-09-24——使用者要求「用這次新增的東西（K線快取／總經加權／基本面
# 硬性過濾）做詳細回測，跟過去策略比較勝率有沒有提升」。沿用上面 full_backtest
# 的背景執行緒＋state_store meta 輪詢模式（同樣道理：這次要跑兩輪TW50回測，
# 時間是full_backtest的兩倍，一定會超過gunicorn逾時，不能同步做）。
_compare_backtest_running = threading.Event()

def _run_compare_backtest_bg(min_score):
    from state_store import store
    if _compare_backtest_running.is_set():
        return
    _compare_backtest_running.set()
    try:
        from backtester import run_comparison_backtest_tw
        store.set_meta("compare_backtest_progress", {"status": "running", "done": 0, "total": 0, "ticker": ""})
        def _cb(done, total, ticker):
            store.set_meta("compare_backtest_progress", {"status": "running", "done": done, "total": total, "ticker": ticker})
        result = run_comparison_backtest_tw(min_score=min_score, progress_cb=_cb)
        store.set_meta("compare_backtest_result", result)
        store.set_meta("compare_backtest_progress", {"status": "done", "done": 1, "total": 1, "ticker": ""})
        logger.info(f"[BT] 策略比較回測完成：勝率 {result['comparison']['win_rate_before']}% → {result['comparison']['win_rate_after']}%")
    except Exception as e:
        logger.error(f"_run_compare_backtest_bg: {e}", exc_info=True)
        store.set_meta("compare_backtest_progress", {"status": "error", "error": str(e)})
    finally:
        _compare_backtest_running.clear()

@app.route("/api/backtest/compare/run", methods=["POST"])
def api_backtest_compare_run():
    if _compare_backtest_running.is_set():
        return jsonify({"status": "already_running"})
    min_score = request.args.get("min_score", default=65.0, type=float)
    threading.Thread(target=_run_compare_backtest_bg, args=(min_score,), daemon=True).start()
    return jsonify({"status": "started", "min_score": min_score,
                     "note": "TW50成分股，新舊策略各跑一輪（約為 full/run 兩倍時間），用 /api/backtest/compare/result 查進度"})

@app.route("/api/backtest/compare/result")
def api_backtest_compare_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("compare_backtest_progress", {"status": "never_run"}),
        "result": store.get_meta("compare_backtest_result", None),
    })

# ★ 新增：2026-09-28——item 3（walk-forward/OOS 驗證放空分層閘門，ChatGPT/
# Perplexity 都建議先驗證新閘門有沒有效，再動 alpha_score）。沿用 compare_backtest
# 同一套背景執行緒＋state_store meta 輪詢模式（同樣要跑兩輪TW50回測，一定會
# 超過 gunicorn 逾時）。
_short_gate_validation_running = threading.Event()

def _run_short_gate_validation_bg(min_score):
    from state_store import store
    if _short_gate_validation_running.is_set():
        return
    _short_gate_validation_running.set()
    try:
        from backtester import run_short_gate_validation_tw
        store.set_meta("short_gate_validation_progress", {"status": "running", "done": 0, "total": 0, "ticker": ""})
        def _cb(done, total, ticker):
            store.set_meta("short_gate_validation_progress", {"status": "running", "done": done, "total": total, "ticker": ticker})
        result = run_short_gate_validation_tw(min_score=min_score, progress_cb=_cb)
        store.set_meta("short_gate_validation_result", result)
        store.set_meta("short_gate_validation_progress", {"status": "done", "done": 1, "total": 1, "ticker": ""})
        c = result.get("comparison", {})
        logger.info(f"[BT] 放空分層閘門驗證完成：放空筆數 {c.get('sell_n_trades_before',0)} → {c.get('sell_n_trades_after',0)}，"
                    f"勝率 {c.get('sell_win_rate_before',0)}% → {c.get('sell_win_rate_after',0)}%")
    except Exception as e:
        logger.error(f"_run_short_gate_validation_bg: {e}", exc_info=True)
        store.set_meta("short_gate_validation_progress", {"status": "error", "error": str(e)})
    finally:
        _short_gate_validation_running.clear()

@app.route("/api/backtest/short_gate_validation/run", methods=["POST"])
def api_backtest_short_gate_validation_run():
    if _short_gate_validation_running.is_set():
        return jsonify({"status": "already_running"})
    min_score = request.args.get("min_score", default=65.0, type=float)
    threading.Thread(target=_run_short_gate_validation_bg, args=(min_score,), daemon=True).start()
    return jsonify({"status": "started", "min_score": min_score,
                     "note": "TW50成分股，新舊放空邏輯各跑一輪，用 /api/backtest/short_gate_validation/result 查進度"})

@app.route("/api/backtest/short_gate_validation/result")
def api_backtest_short_gate_validation_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("short_gate_validation_progress", {"status": "never_run"}),
        "result": store.get_meta("short_gate_validation_result", None),
    })

# ★ 新增：2026-09-29——item：涵蓋真實空頭期間。上面 short_gate_validation 的
# 樣本被 production fetch_ohlcv() 綁死在近1年（幾乎全程bullish regime），
# ChatGPT/Perplexity都指出「放空筆數0」這個結果沒辦法區分「閘門真的有效」
# 還是「近1年根本沒出現過空頭條件」。這裡用 backtester.run_short_gate_
# validation_tw_period()（抓長期歷史、只截到指定期間）跑2022年台股真實空頭，
# 同一套背景執行緒＋state_store meta輪詢模式（50檔×長期歷史抓取+雙輪回測，
# 一定超過 gunicorn 逾時）。
_short_gate_validation_bear_running = threading.Event()

def _run_short_gate_validation_bear_bg(min_score, period_start, period_end):
    from state_store import store
    if _short_gate_validation_bear_running.is_set():
        return
    _short_gate_validation_bear_running.set()
    try:
        from backtester import run_short_gate_validation_tw_period
        store.set_meta("short_gate_validation_bear_progress", {"status": "running", "done": 0, "total": 0, "ticker": ""})
        def _cb(done, total, ticker):
            store.set_meta("short_gate_validation_bear_progress", {"status": "running", "done": done, "total": total, "ticker": ticker})
        result = run_short_gate_validation_tw_period(min_score=min_score, period_start=period_start,
                                                       period_end=period_end, progress_cb=_cb)
        store.set_meta("short_gate_validation_bear_result", result)
        store.set_meta("short_gate_validation_bear_progress", {"status": "done", "done": 1, "total": 1, "ticker": ""})
        c = result.get("comparison", {})
        logger.info(f"[BT] 空頭期間({period_start}~{period_end})放空分層閘門驗證完成：放空筆數 "
                    f"{c.get('sell_n_trades_before',0)} → {c.get('sell_n_trades_after',0)}，"
                    f"勝率 {c.get('sell_win_rate_before',0)}% → {c.get('sell_win_rate_after',0)}%")
    except Exception as e:
        logger.error(f"_run_short_gate_validation_bear_bg: {e}", exc_info=True)
        store.set_meta("short_gate_validation_bear_progress", {"status": "error", "error": str(e)})
    finally:
        _short_gate_validation_bear_running.clear()

@app.route("/api/backtest/short_gate_validation_bear/run", methods=["POST"])
def api_backtest_short_gate_validation_bear_run():
    if _short_gate_validation_bear_running.is_set():
        return jsonify({"status": "already_running"})
    min_score = request.args.get("min_score", default=65.0, type=float)
    period_start = request.args.get("period_start", default="2022-01-01", type=str)
    period_end = request.args.get("period_end", default="2022-12-31", type=str)
    threading.Thread(target=_run_short_gate_validation_bear_bg, args=(min_score, period_start, period_end), daemon=True).start()
    return jsonify({"status": "started", "min_score": min_score, "period_start": period_start, "period_end": period_end,
                     "note": "TW50成分股，用長期歷史截到指定期間，新舊放空邏輯各跑一輪，用 /api/backtest/short_gate_validation_bear/result 查進度"})

@app.route("/api/backtest/short_gate_validation_bear/result")
def api_backtest_short_gate_validation_bear_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("short_gate_validation_bear_progress", {"status": "never_run"}),
        "result": store.get_meta("short_gate_validation_bear_result", None),
    })

# ★ 新增：2026-09-24——使用者看到第一版比較回測（只有TWII一項）差異很小之後，
# 明確要求「用真實資料回測，不要假數據，能補的都補上」。融資餘額、個股月營收
# 這兩項都查證出有真實的官方歷史資料來源可以回填（見 data_fetcher.
# backfill_margin_history() / fundamentals.backfill_monthly_revenue_history()
# 的說明），但這兩個回填都要對 TWSE/MOPS 發出幾百次逐日/逐月請求，一定會超過
# gunicorn 逾時，所以比照 full_backtest / compare_backtest 同一套「背景執行緒
# ＋ state_store meta 輪詢進度」模式，兩個回填包在同一個背景工作裡依序執行
# （先融資、再月營收），一次觸發即可，不用管理兩條獨立的背景執行緒。
_backfill_running = threading.Event()

def _run_backfill_bg(margin_days_back, revenue_months_back):
    from state_store import store
    if _backfill_running.is_set():
        return
    _backfill_running.set()
    try:
        from data_fetcher import backfill_margin_history
        from fundamentals import backfill_monthly_revenue_history
        store.set_meta("backfill_progress", {"status": "running", "stage": "margin", "done": 0, "total": 0, "item": ""})
        def _cb_margin(done, total, item):
            store.set_meta("backfill_progress", {"status": "running", "stage": "margin", "done": done, "total": total, "item": item})
        margin_result = backfill_margin_history(days_back=margin_days_back, progress_cb=_cb_margin)
        store.set_meta("backfill_margin_result", margin_result)
        store.set_meta("backfill_progress", {"status": "running", "stage": "monthly_revenue", "done": 0, "total": 0, "item": ""})
        def _cb_rev(done, total, item):
            store.set_meta("backfill_progress", {"status": "running", "stage": "monthly_revenue", "done": done, "total": total, "item": item})
        revenue_result = backfill_monthly_revenue_history(months_back=revenue_months_back, progress_cb=_cb_rev)
        store.set_meta("backfill_revenue_result", revenue_result)
        store.set_meta("backfill_progress", {"status": "done", "stage": "done", "done": 1, "total": 1, "item": ""})
        logger.info(f"[BACKFILL] 完成：融資 {margin_result.get('days_fetched',0)}天、"
                    f"月營收 {revenue_result.get('total_rows_upserted',0)}筆（{revenue_result.get('months_fetched',0)}個月）")
    except Exception as e:
        logger.error(f"_run_backfill_bg: {e}", exc_info=True)
        store.set_meta("backfill_progress", {"status": "error", "error": str(e)})
    finally:
        _backfill_running.clear()

@app.route("/api/backfill/run", methods=["POST"])
def api_backfill_run():
    if _backfill_running.is_set():
        return jsonify({"status": "already_running"})
    margin_days_back = request.args.get("margin_days_back", default=400, type=int)
    revenue_months_back = request.args.get("revenue_months_back", default=15, type=int)
    threading.Thread(target=_run_backfill_bg, args=(margin_days_back, revenue_months_back), daemon=True).start()
    return jsonify({"status": "started", "margin_days_back": margin_days_back, "revenue_months_back": revenue_months_back,
                     "note": "依序回填融資餘額歷史(TWSE MI_MARGN)、個股月營收歷史(MOPS)，會需要一段時間"
                             "（融資約幾百次逐日請求、月營收約幾十次逐月請求，中間都有延遲避免對官方站造成負擔），"
                             "用 /api/backfill/result 查進度"})

@app.route("/api/backfill/result")
def api_backfill_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("backfill_progress", {"status": "never_run"}),
        "margin_result": store.get_meta("backfill_margin_result", None),
        "revenue_result": store.get_meta("backfill_revenue_result", None),
    })

# ★ 新增：2026-09-26——稽核報告中長期項目「真正的分批出場回測邏輯」。比照
# 上面 full_backtest/compare_backtest/backfill 同一套「背景執行緒＋
# state_store meta 輪詢進度」模式（TW50全市場分批出場回測時間跟一般全市場
# 回測差不多量級，仍會超過gunicorn同步逾時）。
_partial_exit_bt_running = threading.Event()

def _run_partial_exit_bt_bg(min_score):
    from state_store import store
    if _partial_exit_bt_running.is_set():
        return
    _partial_exit_bt_running.set()
    try:
        from backtester import run_full_backtest_tw_partial
        store.set_meta("partial_exit_bt_progress", {"status": "running", "done": 0, "total": 0, "ticker": ""})
        def _cb(done, total, ticker):
            store.set_meta("partial_exit_bt_progress", {"status": "running", "done": done, "total": total, "ticker": ticker})
        result = run_full_backtest_tw_partial(min_score=min_score, progress_cb=_cb)
        store.set_meta("partial_exit_bt_result", result)
        store.set_meta("partial_exit_bt_progress", {"status": "done", "done": result.get("total", 0), "total": result.get("total", 0), "ticker": ""})
    except Exception as e:
        logger.error(f"_run_partial_exit_bt_bg: {e}", exc_info=True)
        store.set_meta("partial_exit_bt_progress", {"status": "error", "error": str(e)})
    finally:
        _partial_exit_bt_running.clear()

@app.route("/api/backtest/partial_exit/run", methods=["POST"])
def api_backtest_partial_exit_run():
    if _partial_exit_bt_running.is_set():
        return jsonify({"status": "already_running"})
    min_score = request.args.get("min_score", default=65.0, type=float)
    threading.Thread(target=_run_partial_exit_bt_bg, args=(min_score,), daemon=True).start()
    return jsonify({"status": "started", "min_score": min_score,
                     "note": "TW50成分股，用真正的「各1/3分批出場＋保本移動停損」模擬（非單一出場價簡化模型），"
                             "背景執行，用 /api/backtest/partial_exit/result 查進度"})

@app.route("/api/backtest/partial_exit/result")
def api_backtest_partial_exit_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("partial_exit_bt_progress", {"status": "never_run"}),
        "result": store.get_meta("partial_exit_bt_result", None),
    })

# ★ 新增：2026-09-26——稽核報告中長期項目「因子/評分消融分析」。同樣比照
# 背景執行緒＋輪詢模式（baseline + 7個因子＝8輪全市場回測，時間約為單次
# full_backtest的8倍）。
_ablation_bt_running = threading.Event()

def _run_ablation_bt_bg(min_score):
    from state_store import store
    if _ablation_bt_running.is_set():
        return
    _ablation_bt_running.set()
    try:
        from backtester import run_factor_ablation_tw
        store.set_meta("ablation_bt_progress", {"status": "running", "stage": 0, "total_stages": 0, "detail": ""})
        def _cb(stage_idx, total_stages, detail):
            store.set_meta("ablation_bt_progress", {"status": "running", "stage": stage_idx, "total_stages": total_stages, "detail": detail})
        result = run_factor_ablation_tw(min_score=min_score, progress_cb=_cb)
        store.set_meta("ablation_bt_result", result)
        store.set_meta("ablation_bt_progress", {"status": "done", "stage": 8, "total_stages": 8, "detail": ""})
    except Exception as e:
        logger.error(f"_run_ablation_bt_bg: {e}", exc_info=True)
        store.set_meta("ablation_bt_progress", {"status": "error", "error": str(e)})
    finally:
        _ablation_bt_running.clear()

@app.route("/api/backtest/ablation/run", methods=["POST"])
def api_backtest_ablation_run():
    if _ablation_bt_running.is_set():
        return jsonify({"status": "already_running"})
    min_score = request.args.get("min_score", default=65.0, type=float)
    threading.Thread(target=_run_ablation_bt_bg, args=(min_score,), daemon=True).start()
    return jsonify({"status": "started", "min_score": min_score,
                     "note": "逐一關閉EMA/半年線/RSI/MACD/量增/ADX/週線confirm 共7個評分因子各跑一次全市場回測，"
                             "量化每個因子對勝率的邊際貢獻，約為單次full_backtest的8倍時間，"
                             "背景執行，用 /api/backtest/ablation/result 查進度"})

@app.route("/api/backtest/ablation/result")
def api_backtest_ablation_result():
    from state_store import store
    return jsonify({
        "progress": store.get_meta("ablation_bt_progress", {"status": "never_run"}),
        "result": store.get_meta("ablation_bt_result", None),
    })

# ── Telegram Webhook ──
@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        from telegram_bot import handle_update, send_message
        update  = request.get_json()
        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        reply   = handle_update(update)
        if reply and chat_id: send_message(chat_id, reply)
    except Exception as e: logger.error(f"telegram_webhook: {e}")
    return jsonify({"ok": True})

@app.route("/api/webhook/set", methods=["POST"])
def set_webhook():
    # ★ 修正：2026-09-03——這個路由原本任何人都能呼叫，可以把機器人的 webhook
    # 重新指向任意網址，等於能劫持所有使用者傳給 bot 的訊息（包含 /start 訂閱、
    # 未來若有付費指令等）。加一層極簡驗證：呼叫者必須在 header 帶出跟
    # TELEGRAM_BOT_TOKEN 相同的值，才允許變更 webhook（機主自己已經知道這組
    # token，一般訪客不會知道）。
    auth = request.headers.get("X-Admin-Token", "")
    if not TELEGRAM_BOT_TOKEN or auth != TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    try:
        from telegram_bot import set_webhook as _sw
        data = request.get_json(); url = data.get("url", "")
        if not url: return jsonify({"error": "url 必填"}), 400
        return jsonify({"ok": _sw(f"{url}/webhook/{TELEGRAM_BOT_TOKEN}")})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/test/telegram", methods=["POST"])
def test_telegram():
    from telegram_bot import send_alert
    send_alert(f"✅ 系統測試 {SYSTEM['name']} v{SYSTEM['version']} 運行正常", "info")
    return jsonify({"status": "sent"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": SYSTEM["version"], "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route("/api/diagnostics/universe_thresholds")
def diagnostics_universe_thresholds():
    """★ 新增：2026-09-26——唯讀診斷端點，回答「均量門檻調到多少張，全市場掃描
    會多納入幾檔股票」，供使用者在真正調整 config.py THRESH['min_avg_volume']
    （目前500張）之前先看到實際影響範圍，不用用猜的。見 stock_universe.py
    get_volume_threshold_distribution() 的說明。"""
    try:
        from stock_universe import get_volume_threshold_distribution
        return jsonify(get_volume_threshold_distribution())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/diagnostics/fundamentals_extra")
def diagnostics_fundamentals_extra():
    """★ 新增：2026-09-27——使用者要求補上「估值面(本益比/股價淨值比/殖利率) +
    獲利品質」與「融券餘額 + 當沖比例（籌碼面風險）」。這兩塊的資料擷取層
    （fundamentals.fetch_valuation_map() / fetch_margin_short_map() /
    fetch_market_daytrading_overlay()，見該檔案說明與檔尾 TODO）已經打通，
    但還沒接進 signal_engine.py 的評分邏輯（設計方向留待使用者確認）。這個
    唯讀診斷端點讓人可以直接看到目前抓得到什麼資料、涵蓋幾檔股票，不用等
    評分邏輯接上才能檢視。query string 可傳 ?code=2330 只看單一檔。"""
    try:
        from fundamentals import fetch_valuation_map, fetch_margin_short_map, fetch_market_daytrading_overlay
        code = (request.args.get("code") or "").strip()
        valuation = fetch_valuation_map()
        margin_short = fetch_margin_short_map()
        overlay = fetch_market_daytrading_overlay()
        if code:
            return jsonify({
                "code": code,
                "valuation": valuation.get(code),
                "margin_short": margin_short.get(code),
                "market_daytrading_overlay": overlay,
            })
        return jsonify({
            "valuation_coverage": len(valuation),
            "margin_short_coverage": len(margin_short),
            "market_daytrading_overlay": overlay,
            "sample_codes": list(valuation.keys())[:5],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/diagnostics/quality_and_news")
def diagnostics_quality_and_news():
    """★ 新增：2026-09-28——使用者要求補上「獲利品質(毛利率/營業利益率趨勢)」
    跟「重大訊息公告(紅旗關鍵字)」，已接進 scanner.py 的評分邏輯（加減分，
    非硬性排除，理由見該檔案對應段落）。這個唯讀診斷端點讓人可以直接看到
    目前抓得到什麼資料、涵蓋幾檔股票，不用等下一次掃描才能檢視。query
    string 可傳 ?code=2330 只看單一檔。"""
    try:
        from fundamentals import fetch_profitability_quality_map, get_profitability_quality, fetch_material_news_risk_map
        code = (request.args.get("code") or "").strip()
        news = fetch_material_news_risk_map()
        if code:
            # 只有帶 ?code= 查單一檔時才呼叫 get_profitability_quality()
            # 做資料庫讀寫算趨勢——不帶 code 的整體檢視絕對不能對全市場
            # 900+ 檔都做一次，那是這次上線後修掉的效能問題（見
            # fundamentals.py fetch_profitability_quality_map() 的說明）。
            return jsonify({
                "code": code,
                "profitability_quality": get_profitability_quality(code),
                "material_news_risk": news.get(code),
            })
        quality = fetch_profitability_quality_map()  # 只抓資料+快取，不碰DB
        return jsonify({
            "profitability_quality_coverage": len(quality),
            "material_news_risk_hits": len(news),
            "sample_quality_codes": list(quality.keys())[:5],
            "sample_news_codes": list(news.keys())[:5],
            "note": "profitability_quality_coverage 是當期資料覆蓋率，不含季度趨勢；"
                    "trend 只在帶 ?code= 查單一檔時才會計算並寫入資料庫。",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/diagnostics/pending_signals")
def diagnostics_pending_signals():
    """★ 新增：2026-09-27——原本是臨時診斷端點，用來查清楚使用者回報的「已有14個
    持倉，暫停新增（上限5）」是不是逾期平倉邏輯失效。查證結果：不是——14筆訊號
    生成當時全部只有3天，遠低於 signal_expire_days=15，本來就還不該被強制平倉；
    真正原因是 run_daily_scan() 裡「單次掃描最多新增幾筆」原本只受
    TELEGRAM_CONFIG["max_signals_per_day"] 限制，跟 MAX_SIMULTANEOUS_POSITIONS
    完全脫鉤，已在 scanner.py 修正（見該檔案 run_daily_scan() 的說明）。這個端點
    留下來當常駐診斷用，方便之後隨時查目前未平倉訊號清單跟各自的天數，不用再
    另外寫一次。"""
    try:
        from state_store import store
        from datetime import datetime, timezone
        pending = store.get_pending_signals()
        now = datetime.now(timezone.utc)
        out = []
        for sig in pending:
            gen_at = sig.get("generated_at", "")
            age_days = None
            try:
                gen_dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                if gen_dt.tzinfo is None:
                    gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                age_days = (now - gen_dt).days
            except Exception:
                pass
            out.append({
                "id": sig.get("id"), "ticker": sig.get("ticker"), "code": sig.get("code"),
                "direction": sig.get("direction"), "status": sig.get("status"), "result": sig.get("result"),
                "stop_loss": sig.get("stop_loss"), "generated_at": gen_at, "age_days": age_days,
            })
        return jsonify({"count": len(out), "signals": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/diagnostics")
def diagnostics():
    def chk(m):
        try: __import__(m); return "✅ 已安裝"
        except: return "❌ 未安裝"
    # ★ 修正：2026-08-30——使用者反覆問「FUGLE_API_KEY 到底有沒有生效」，之前完全沒有
    #   任何一個地方可以直接查證，只能翻 log、看有沒有出現 fugle 來源的資料，很不透明。
    #   這裡直接檢查金鑰是否存在，並且實際打一次富果 API 驗證金鑰真的能用（不是只檢查
    #   環境變數有沒有設定——設定了但打錯字、額度用完、金鑰失效這些情況，只檢查「有沒有
    #   設定」是看不出來的）。金鑰本身不外洩，只回傳有效/無效的判斷結果。
    fugle_key_set = bool(os.getenv("FUGLE_API_KEY", ""))
    fugle_status = "⚠️ 未設定 FUGLE_API_KEY"
    if fugle_key_set:
        try:
            # ★ 修正：2026-08-30——改用專門的 fugle_key_diagnostic()，用「2330」這個沒有
            #   爭議的真實股票代碼單獨測金鑰本身有沒有效，不再跟 TAIEX/TPEx 這兩個指數
            #   代碼是否被富果接受混在一起判斷，避免誤導。同時把實際 HTTP 狀態碼顯示出來。
            from fubon_broker import fugle_key_diagnostic
            _test = fugle_key_diagnostic()
            fugle_status = ("✅ " if _test["ok"] else "❌ ") + _test["detail"]
        except Exception as e:
            fugle_status = f"❌ 金鑰已設定，但檢測過程發生錯誤：{e}"
    # ★ 修正：2026-09-03——使用者反映「訊號網站看得到、Telegram 卻收不到」，但
    #   之前 /api/diagnostics 完全沒有揭露 Telegram 端的設定狀態（TELEGRAM_CHAT_ID
    #   有沒有設定、訂閱名單裡有幾個人、admin 名單裡有沒有真的包含機主自己）。
    #   這裡補上這些資訊（不外洩實際 chat_id 數值，只回傳布林值/數量），
    #   讓機主自己就能查證設定是否正確，不用再靠翻 log 用猜的。
    telegram_status = {
        "telegram_chat_id_set":      bool(TELEGRAM_CHAT_ID),
        "telegram_free_channel_set": bool(TELEGRAM_FREE_CHANNEL),
        "telegram_paid_channel_set": bool(TELEGRAM_PAID_CHANNEL),
    }
    try:
        from telegram_bot import get_subscriber_counts
        telegram_status["subscribers"] = get_subscriber_counts()
    except Exception as e:
        telegram_status["subscribers"] = f"❌ 讀取失敗：{e}"
    # ★ 新增：2026-09-24——讓K線持久化快取「有沒有真的在運作」可以直接從
    # 網站查證，不用翻資料庫。見 state_store.get_ohlcv_cache_stats()。
    try:
        from state_store import store
        ohlcv_cache_status = store.get_ohlcv_cache_stats()
    except Exception as e:
        ohlcv_cache_status = {"error": str(e)}
    # ★ 新增：2026-09-24——同理，讓融資餘額/月營收歷史回填「到底有沒有真的
    # 回填到資料」也能直接從網站查證。
    try:
        from state_store import store
        margin_dates = store.get_margin_chg_dates_covered()
        revenue_periods = store.get_monthly_revenue_periods_covered()
        macro_backfill_status = {
            "margin_days_covered": len(margin_dates),
            "margin_date_range": [margin_dates[0], margin_dates[-1]] if margin_dates else [],
            "revenue_months_covered": len(revenue_periods),
            "revenue_period_range": [revenue_periods[0], revenue_periods[-1]] if revenue_periods else [],
        }
    except Exception as e:
        macro_backfill_status = {"error": str(e)}
    return jsonify({
        "python":           sys.version[:20],
        "yfinance":         chk("yfinance"),
        "apscheduler":      chk("apscheduler"),
        "requests":         chk("requests"),
        "flask":            chk("flask"),
        "telegram_token":   bool(TELEGRAM_BOT_TOKEN),
        **telegram_status,
        "fugle_api_key":    fugle_status,
        "ohlcv_cache":      ohlcv_cache_status,
        "macro_backfill":   macro_backfill_status,
        "template_dir":     TEMPLATE_DIR,
        "template_exists":  os.path.exists(os.path.join(TEMPLATE_DIR, "dashboard.html")),
        "scheduler_running":SCHEDULER_OK and scheduler.running if SCHEDULER_OK else False,
        "scheduled_jobs":  [j.id for j in scheduler.get_jobs()] if SCHEDULER_OK and scheduler.running else [],
    })

# ── 啟動 ──
def create_app():
    os.makedirs("instance", exist_ok=True)
    setup_scheduler()
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if webhook_url and TELEGRAM_BOT_TOKEN:
        try:
            from telegram_bot import set_webhook
            set_webhook(f"{webhook_url}/webhook/{TELEGRAM_BOT_TOKEN}")
        except Exception as e: logger.warning(f"Webhook 設定失敗: {e}")
    logger.info(f"✅ {SYSTEM['name']} v{SYSTEM['version']} 啟動")
    return app

app = create_app()

# ── 官方成交量歷史回補（背景、單飛、失敗容忍）──────────────────────
_ov_bf_lock = __import__("threading").Lock()
_ov_bf_tried = {}


def _backfill_official_volume(code, is_otc, months=4):
    """證交所 STOCK_DAY／櫃買 st43 逐月抓個股官方成交量（張）寫入 official_volume。失敗只記 log。"""
    import time as _t, requests as _rq
    from datetime import datetime as _dt, timedelta as _td
    from state_store import store as _st
    rows = []
    now = _dt.utcnow() + _td(hours=8)
    y, m = now.year, now.month
    hdr = {"User-Agent": "Mozilla/5.0"}
    for _ in range(months):
        try:
            if not is_otc:
                r = _rq.get("https://www.twse.com.tw/exchangeReport/STOCK_DAY",
                            params={"response": "json", "date": f"{y}{m:02d}01", "stockNo": code},
                            headers=hdr, timeout=(5, 15))
                for x in (r.json().get("data") or []):
                    yy, mm, dd = x[0].split("/")
                    rows.append((code, f"{int(yy)+1911}-{mm}-{dd}", int(x[1].replace(",", "")) // 1000))
            else:
                r = _rq.get("https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php",
                            params={"l": "zh-tw", "d": f"{y-1911}/{m:02d}", "stkno": code},
                            headers=hdr, timeout=(5, 15))
                for x in (r.json().get("aaData") or []):
                    yy, mm, dd = x[0].split("/")
                    rows.append((code, f"{int(yy)+1911}-{mm}-{dd}", int(float(x[1].replace(",", "")))))
        except Exception as e:
            logger.warning(f"official volume backfill {code} {y}-{m}: {e}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
        _t.sleep(0.6)
    if rows:
        try:
            _st.upsert_official_volumes(rows)
            logger.info(f"official volume backfill {code}: {len(rows)} days")
        except Exception as e:
            logger.warning(f"upsert official volume {code}: {e}")


def _kick_backfill(code, ticker, n_have):
    import time as _t, threading as _th
    if n_have >= 60 or _t.time() - _ov_bf_tried.get(code, 0) < 6 * 3600:
        return
    _ov_bf_tried[code] = _t.time()
    def _run():
        if _ov_bf_lock.acquire(blocking=False):
            try:
                _backfill_official_volume(code, ticker.endswith(".TWO"))
            finally:
                _ov_bf_lock.release()
    _th.Thread(target=_run, daemon=True).start()



def _warm_caches():
    """部署／重啟後第一位訪客原本要等約 35 秒（冷快取要同時抓大盤、國際指數、全市場清單）。
    啟動後背景先抓一次，讓使用者打開網頁時已經是暖的。失敗不影響服務。"""
    import time as _t
    _t.sleep(5)
    for name, fn in (("market_overview", lambda: __import__("data_fetcher").fetch_market_overview()),
                     ("universe", lambda: __import__("stock_universe").build_full_universe()),
                     ("calendar", lambda: __import__("calendar_events").get_ex_dividend())):
        try:
            fn(); logger.info(f"預熱完成：{name}")
        except Exception as e:
            logger.warning(f"預熱失敗 {name}: {e}")

try:
    import threading as _wth
    _wth.Thread(target=_warm_caches, daemon=True).start()
    def _inst_boot():
        import time as _t2
        _t2.sleep(90)
        job_collect_inst()
    _wth.Thread(target=_inst_boot, daemon=True).start()
except Exception:
    pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SYSTEM["web_port"], debug=False)
