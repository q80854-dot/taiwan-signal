"""
app.py — Flask 主應用 v1.0
排程：08:45 早報 / 16:00 更新品種 / 16:30 掃描
API + Telegram Webhook + 系統監控
"""
import sys, os, logging, threading
print("=== APP STARTING ===", flush=True)
print(f"Python: {sys.version}", flush=True)

from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template
from config import SYSTEM, TELEGRAM_BOT_TOKEN

logging.basicConfig(
    level=getattr(logging, SYSTEM["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

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
    scheduler.add_job(job_morning_brief,   CronTrigger(hour=8,  minute=45, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="morning_brief",    replace_existing=True)
    scheduler.add_job(job_refresh_universe,CronTrigger(hour=16, minute=0,  day_of_week="mon-fri", timezone=TZ_TAIPEI), id="refresh_universe", replace_existing=True)
    scheduler.add_job(job_daily_scan,      CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=TZ_TAIPEI), id="daily_scan",       replace_existing=True)
    scheduler.add_job(lambda: logger.debug("❤️ 心跳"), "interval", hours=1, id="heartbeat")
    scheduler.start()
    logger.info("✅ 排程器已啟動")

# ── Routes ──
@app.route("/")
def index():
    try: return render_template("dashboard.html")
    except: return jsonify({"status":"ok","system":SYSTEM["name"],"version":SYSTEM["version"]})

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
            "version":         SYSTEM["version"],
            "system_name":     SYSTEM["name"],
            "scan_count":      scan_st["scan_count"],
            "signal_count":    scan_st["signal_count"],
            "last_scan_time":  scan_st["last_scan_at"],
            "active_signals":  scan_st["signals"][:15],
            "market_overview": market,
            "market_session":  session,
            "win_rate":        perf,
            "system_status":   sys_stat,
            "sentiment_score": market.get("sentiment_score",50),
            "sentiment_zh":    market.get("sentiment_zh","中性"),
        })
    except Exception as e:
        logger.error(f"api_state: {e}"); return jsonify({"error":str(e)}), 500

@app.route("/api/signals")
def api_signals():
    from state_store import store
    limit     = int(request.args.get("limit",20))
    direction = request.args.get("direction","")
    sector    = request.args.get("sector","")
    signals   = store.get_recent_signals(limit=limit*2)
    if direction: signals=[s for s in signals if s.get("direction")==direction]
    if sector:    signals=[s for s in signals if s.get("sector")==sector]
    return jsonify({"signals":signals[:limit],"count":len(signals[:limit])})

@app.route("/api/market")
def api_market():
    try:
        from data_fetcher import fetch_market_overview
        return jsonify(fetch_market_overview())
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/universe")
def api_universe():
    try:
        from stock_universe import build_universe
        df = build_universe()
        return jsonify({"count":len(df),"sectors":df.groupby("sector").size().to_dict()})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/scan/force", methods=["POST"])
def api_force_scan():
    threading.Thread(target=job_daily_scan, daemon=True).start()
    return jsonify({"status":"scanning_started"})

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
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/backtest/<ticker>/walkforward")
def api_walkforward(ticker: str):
    try:
        from backtester import walk_forward_backtest_tw
        t = ticker.upper()
        if ".TW" not in t: t += ".TW"
        return jsonify(walk_forward_backtest_tw(t))
    except Exception as e: return jsonify({"error":str(e)}), 500

# ── Telegram Webhook ──
@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        from telegram_bot import handle_update, send_message
        update  = request.get_json()
        msg     = update.get("message",{})
        chat_id = str(msg.get("chat",{}).get("id",""))
        reply   = handle_update(update)
        if reply and chat_id: send_message(chat_id, reply)
    except Exception as e: logger.error(f"telegram_webhook: {e}")
    return jsonify({"ok":True})

@app.route("/api/webhook/set", methods=["POST"])
def set_webhook():
    try:
        from telegram_bot import set_webhook as _sw
        data = request.get_json(); url = data.get("url","")
        if not url: return jsonify({"error":"url 必填"}), 400
        return jsonify({"ok":_sw(f"{url}/webhook/{TELEGRAM_BOT_TOKEN}")})
    except Exception as e: return jsonify({"error":str(e)}), 500

@app.route("/api/test/telegram", methods=["POST"])
def test_telegram():
    from telegram_bot import send_alert
    send_alert(f"✅ 系統測試 {SYSTEM['name']} v{SYSTEM['version']} 運行正常","info")
    return jsonify({"status":"sent"})

@app.route("/health")
def health():
    return jsonify({"status":"ok","version":SYSTEM["version"],"timestamp":datetime.now(timezone.utc).isoformat()})

@app.route("/api/diagnostics")
def diagnostics():
    import sys
    def chk(m):
        try: __import__(m); return "✅ 已安裝"
        except: return "❌ 未安裝"
    return jsonify({
        "python":         sys.version[:20],
        "yfinance":       chk("yfinance"),
        "apscheduler":    chk("apscheduler"),
        "requests":       chk("requests"),
        "pandas":         chk("pandas"),
        "flask":          chk("flask"),
        "telegram_token": bool(TELEGRAM_BOT_TOKEN),
        "scheduler_running": SCHEDULER_OK and scheduler.running if SCHEDULER_OK else False,
        "scheduled_jobs": [j.id for j in scheduler.get_jobs()] if SCHEDULER_OK and scheduler.running else [],
    })

# ── 啟動 ──
def create_app():
    os.makedirs("instance", exist_ok=True)
    setup_scheduler()
    webhook_url = os.getenv("RENDER_EXTERNAL_URL","")
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
