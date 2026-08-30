"""
app.py — Flask 主應用 v1.2
修正：改用 send_from_directory 繞過 Jinja2 解析 dashboard.html
"""
import sys, os, logging, threading
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_from_directory
from config import SYSTEM, TELEGRAM_BOT_TOKEN

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
    logger.info("⏰ 早盤摘要")
    try:
        from data_fetcher import fetch_market_overview
        from telegram_bot import send_morning_brief
        send_morning_brief(fetch_market_overview())
    except Exception as e: logger.error(f"job_morning_brief: {e}")

def job_daily_scan():
    logger.info("⏰ 全市場掃描")
    try:
        from scanner import scanner
        scanner.run_daily_scan()
    except Exception as e: logger.error(f"job_daily_scan: {e}", exc_info=True)

def job_refresh_universe():
    logger.info("⏰ 品種清單更新")
    try:
        from stock_universe import refresh_universe_daily
        refresh_universe_daily()
    except Exception as e: logger.error(f"job_refresh_universe: {e}")

def setup_scheduler():
    if not SCHEDULER_OK: return
    scheduler.add_job(job_morning_brief,    CronTrigger(hour=8,  minute=45, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="morning_brief",    replace_existing=True)
    scheduler.add_job(job_refresh_universe, CronTrigger(hour=16, minute=0,  day_of_week="mon-fri", timezone=TZ_TAIPEI), id="refresh_universe", replace_existing=True)
    scheduler.add_job(job_daily_scan,       CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="daily_scan",       replace_existing=True)
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

# ── 批量回測（多檔一次跑）──
# ★ 新增：2026-08-30——使用者要求一次回測 30+ 檔，逐檔呼叫 /api/backtest/<ticker>
#   會很慢（每檔都要重新抓資料、重新算指標），而且 30+ 檔跑完常常超過
#   gunicorn 的 120 秒逾時。這裡改成背景執行緒（跟 /api/scan/force 同一個模式）：
#   POST 啟動後立刻回應，前端/呼叫端改用 /api/backtest/full/status 輪詢結果，
#   避免長時間佔用一個 HTTP request、也避免逾時被切斷導致跑到一半的結果整組遺失。
#   backtester.py 的 run_full_backtest_tw() 本來就有批量回測的邏輯，這裡只是把它
#   接上 API，沒有新增或修改回測本身的計算邏輯。
_full_backtest_state = {"status": "idle", "started_at": None, "result": None, "error": None}

def job_full_backtest(tickers, min_score):
    global _full_backtest_state
    try:
        from backtester import run_full_backtest_tw
        result = run_full_backtest_tw(tickers=tickers, min_score=min_score)
        _full_backtest_state["result"] = result
        _full_backtest_state["status"] = "done"
        _full_backtest_state["error"] = None
        logger.info(f"[BT-full] 完成，共 {result.get('total',0)} 檔有效結果")
    except Exception as e:
        logger.error(f"job_full_backtest: {e}", exc_info=True)
        _full_backtest_state["status"] = "error"
        _full_backtest_state["error"] = str(e)

@app.route("/api/backtest/full", methods=["POST"])
def api_backtest_full_start():
    if _full_backtest_state["status"] == "running":
        return jsonify({"status": "already_running", "started_at": _full_backtest_state["started_at"]})
    data = request.get_json(silent=True) or {}
    tickers = data.get("tickers")
    min_score = data.get("min_score", 65.0)
    if tickers is not None and not isinstance(tickers, list):
        return jsonify({"error": "tickers 必須是陣列"}), 400
    _full_backtest_state["status"] = "running"
    _full_backtest_state["started_at"] = datetime.now(timezone.utc).isoformat()
    _full_backtest_state["result"] = None
    _full_backtest_state["error"] = None
    threading.Thread(target=job_full_backtest, args=(tickers, min_score), daemon=True).start()
    return jsonify({"status": "started", "n_tickers": len(tickers) if tickers else "default(TW50)"})

@app.route("/api/backtest/full/status")
def api_backtest_full_status():
    return jsonify(_full_backtest_state)

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
    return jsonify({
        "python":           sys.version[:20],
        "yfinance":         chk("yfinance"),
        "apscheduler":      chk("apscheduler"),
        "requests":         chk("requests"),
        "flask":            chk("flask"),
        "telegram_token":   bool(TELEGRAM_BOT_TOKEN),
        "fugle_api_key":    fugle_status,
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
