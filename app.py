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

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

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
        from scanner import scanner
        scanner.run_daily_scan()
    except Exception as e: logger.error(f"job_daily_scan: {e}", exc_info=True)

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
    logger.info("⏰ 盤中安全網檢查")
    try:
        from scanner import scanner
        scanner.check_intraday_price_alerts()
    except Exception as e: logger.error(f"job_intraday_check: {e}", exc_info=True)

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
    scheduler.add_job(job_morning_brief,    CronTrigger(hour=8,  minute=45, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="morning_brief",    replace_existing=True)
    # ★ 新增：2026-09-16——盤中安全網，查已追蹤訊號是否到價，見 job_intraday_check() 說明。
    # ★ 調整：2026-09-19——使用者反映盤中只查4次（10/11/12/13點整）太少、涵蓋不到
    # 9:00-10:00開盤這一小時、也涵蓋不到13:00-13:30收盤前這半小時，而且兩次檢查
    # 間隔長達1小時，反應太慢。改成 09:00-13:30 全交易時段每30分鐘查一次（共10次：
    # 9:00/9:30/10:00/10:30/11:00/11:30/12:00/12:30/13:00/13:30），完整涵蓋開盤到
    # 收盤。這仍然只是「現價 vs 已追蹤訊號的SL/TP」輕量比對（通常<5檔），不是重新
    # 掃描全市場找新訊號——為什麼不能在盤中重新掃描找新訊號，見 scanner.py
    # check_intraday_price_alerts() 開頭的說明。
    scheduler.add_job(job_intraday_check,   CronTrigger(hour="9-13", minute="0,30", day_of_week="mon-fri", timezone=TZ_TAIPEI), id="intraday_check", replace_existing=True)
    scheduler.add_job(job_refresh_universe, CronTrigger(hour=16, minute=0,  day_of_week="mon-fri", timezone=TZ_TAIPEI), id="refresh_universe", replace_existing=True)
    # ★ 新增：2026-09-18——見 job_pre_scan_restart() 說明，在每日掃描前主動
    # 重啟一次 worker、重置記憶體基準，預防跟 2026-09-03/2026-09-18 同一種
    # 掃描到一半被 Render 平台強制重啟、訊號整批遺失的問題。
    scheduler.add_job(job_pre_scan_restart, CronTrigger(hour=16, minute=15, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="pre_scan_restart",  replace_existing=True)
    scheduler.add_job(job_daily_scan,       CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="daily_scan",       replace_existing=True)
    scheduler.add_job(job_scan_watchdog,    CronTrigger(hour=17, minute=0,  day_of_week="mon-fri", timezone=TZ_TAIPEI), id="scan_watchdog",   replace_existing=True)
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
        return jsonify({
            "version":        SYSTEM["version"],
            "system_name":    SYSTEM["name"],
            "scan_count":     scan_st["scan_count"],
            "signal_count":   scan_st["signal_count"],
            "last_scan_time": scan_st["last_scan_at"],
            "active_signals": scan_st["signals"][:15],
            "market_overview":market,
            "market_session": session,
            "win_rate":       perf,
            "system_status":  sys_stat,
            "sentiment_score":market.get("sentiment_score", 50),
            "sentiment_zh":   market.get("sentiment_zh", "中性"),
        })
    except Exception as e:
        logger.error(f"api_state: {e}"); return jsonify({"error": str(e)}), 500

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

def _run_full_backtest_bg(min_score):
    from state_store import store
    if _full_backtest_running.is_set():
        return
    _full_backtest_running.set()
    try:
        from backtester import run_full_backtest_tw
        store.set_meta("full_backtest_progress", {"status": "running", "done": 0, "total": 0, "ticker": ""})
        def _cb(done, total, ticker):
            store.set_meta("full_backtest_progress", {"status": "running", "done": done, "total": total, "ticker": ticker})
        result = run_full_backtest_tw(min_score=min_score, progress_cb=_cb)
        store.set_meta("full_backtest_result", result)
        store.set_meta("full_backtest_progress", {"status": "done", "done": result.get("total", 0), "total": result.get("total", 0), "ticker": ""})
        logger.info(f"[BT] 批量回測完成，共 {result.get('total',0)} 檔有效結果")
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
    threading.Thread(target=_run_full_backtest_bg, args=(min_score,), daemon=True).start()
    return jsonify({"status": "started", "min_score": min_score, "note": "TW50成分股，背景執行，用 /api/backtest/full/result 查進度"})

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=SYSTEM["web_port"], debug=False)
