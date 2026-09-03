"""
config.py — 台股波段智慧交易系統 v1.0
"""
import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_FREE_CHANNEL = os.getenv("TELEGRAM_FREE_CHANNEL", "")
TELEGRAM_PAID_CHANNEL = os.getenv("TELEGRAM_PAID_CHANNEL", "")

FUBON_API_KEY    = os.getenv("FUBON_API_KEY", "")
FUBON_API_SECRET = os.getenv("FUBON_API_SECRET", "")
FUBON_ACCOUNT    = os.getenv("FUBON_ACCOUNT", "")
FINNHUB_API_KEY  = os.getenv("FINNHUB_API_KEY", "")
FRED_API_KEY     = os.getenv("FRED_API_KEY", "")

ACCOUNT_BALANCE_TWD        = float(os.getenv("ACCOUNT_BALANCE_TWD", "500000"))
MAX_RISK_PER_TRADE         = 0.02
MAX_DAILY_RISK             = 0.06
MAX_SIMULTANEOUS_POSITIONS = 5

COMMISSION_RATE  = 0.001425
TAX_RATE_SELL    = 0.003
SHARES_PER_LOT   = 1000
MIN_COMMISSION   = 20

SCAN_TIME      = "16:30"
SCAN_TIMEZONE  = "Asia/Taipei"
MARKET_OPEN    = "09:00"
MARKET_CLOSE   = "13:30"

# ★ 修正：ema_trend 從 120 改成 60（需要 65 根，大多數股票都有）
INDICATOR_PARAMS = {
    "ema_fast": 10, "ema_mid": 20, "ema_slow": 60, "ema_trend": 60,
    "rsi_period": 14, "rsi_overbought": 70, "rsi_oversold": 30,
    "rsi_bull_zone": 50, "rsi_bear_zone": 50,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "bb_period": 20, "bb_std": 2,
    "atr_period": 14, "adx_period": 14, "adx_min": 20,
    "vol_period": 20, "vol_surge_ratio": 1.5,
    "support_lookback": 60,
    "fib_levels": [0.236, 0.382, 0.5, 0.618, 0.786],
    "max_hold_days": 20, "min_hold_days": 3,
}
IND = INDICATOR_PARAMS

# ★ 修正：calc_all_indicators() 不分時框統一要求 ema_trend(120)+5=125 根K線才會判定 valid。
#   原本 weekly 只抓 100 根、hourly 只抓約 90 根(20天×4~5根/天)，兩者都低於125根，
#   代表週線/小時線的指標「一定」是 valid=False——週線確認趨勢(這是本策略文件開頭寫的核心邏輯)
#   實際上從來沒有真正生效過，check_multi_timeframe_tw() 裡的 weekly_bias 永遠是 "neutral"。
#   這裡把週線/小時線改成抓足夠的根數，讓多時框判斷真正跑得動。
TIMEFRAMES = {
    "weekly": {"interval": "1wk", "period": "5y",  "bars": 200, "label": "週線"},
    "daily":  {"interval": "1d",  "period": "1y",  "bars": 250, "label": "日線"},
    "hourly": {"interval": "1h",  "period": "90d", "bars": 200, "label": "小時線"},
}

SIGNAL_THRESHOLDS = {
    "min_score": 65, "high_conf": 80,
    "min_rr": 1.5, "min_adx": 20,
    "min_vol_ratio": 1.2,
    "min_market_cap": 5e8,
    "min_avg_volume": 500,
}
THRESH = SIGNAL_THRESHOLDS

SWING_PARAMS = {
    "大型股": {"sl_atr_mult": 1.5, "tp1_rr": 1.5, "tp2_rr": 2.5, "tp3_rr": 4.0, "trail_stop": True},
    "中型股": {"sl_atr_mult": 2.0, "tp1_rr": 2.0, "tp2_rr": 3.0, "tp3_rr": 5.0, "trail_stop": True},
    "小型股": {"sl_atr_mult": 2.5, "tp1_rr": 2.0, "tp2_rr": 4.0, "tp3_rr": 6.0, "trail_stop": False},
    "ETF":   {"sl_atr_mult": 1.2, "tp1_rr": 1.5, "tp2_rr": 2.0, "tp3_rr": 3.0, "trail_stop": True},
}

CIRCUIT_BREAKER = {
    "twii_drop_stop": -3.5, "twii_drop_caution": -2.0,
    "vix_extreme": 40, "vix_high": 30,
    "foreign_sell_stop": -50e8, "foreign_sell_caution": -20e8,
    "margin_change_warning": -5.0,
    "max_daily_signals": 10, "signal_expire_days": 3,
}
CB = CIRCUIT_BREAKER

CORRELATION_GROUPS = [
    ["半導體", "AI概念", "電子零組件"],
    ["金融保險"],
    ["航運"],
    ["生技醫療"],
    ["ETF"],
]

WATCHLIST_CORE = {
    "0050.TW":   {"name": "元大台灣50",      "cat": "ETF",   "emoji": "📊", "priority": 1},
    "0056.TW":   {"name": "元大高股息",       "cat": "ETF",   "emoji": "💰", "priority": 1},
    "00878.TW":  {"name": "國泰永續高股息",   "cat": "ETF",   "emoji": "🌱", "priority": 1},
    "2330.TW":   {"name": "台積電",  "cat": "半導體",  "emoji": "💎", "priority": 1},
    "2454.TW":   {"name": "聯發科",  "cat": "半導體",  "emoji": "📱", "priority": 1},
    "2317.TW":   {"name": "鴻海",    "cat": "電子零組件","emoji": "🏭", "priority": 1},
    "2308.TW":   {"name": "台達電",  "cat": "電子零組件","emoji": "⚡", "priority": 1},
    "2382.TW":   {"name": "廣達",    "cat": "AI概念",  "emoji": "💻", "priority": 1},
    "6669.TW":   {"name": "緯穎",    "cat": "AI概念",  "emoji": "🤖", "priority": 1},
    "2376.TW":   {"name": "技嘉",    "cat": "AI概念",  "emoji": "🤖", "priority": 1},
    "2303.TW":   {"name": "聯電",    "cat": "半導體",  "emoji": "🔧", "priority": 2},
    "3711.TW":   {"name": "日月光",  "cat": "半導體",  "emoji": "⚡", "priority": 2},
    "2882.TW":   {"name": "國泰金",  "cat": "金融保險","emoji": "🏦", "priority": 2},
    "2881.TW":   {"name": "富邦金",  "cat": "金融保險","emoji": "🏦", "priority": 2},
    "2603.TW":   {"name": "長榮",    "cat": "航運",    "emoji": "🚢", "priority": 2},
    "2615.TW":   {"name": "萬海",    "cat": "航運",    "emoji": "⚓", "priority": 2},
}

TELEGRAM_CONFIG = {
    "free_signal_fields":  ["symbol","name","direction","score_grade","entry_zone","stop_loss","tp1","reason_brief"],
    "paid_signal_fields":  ["symbol","name","direction","score","entry_price","stop_loss","tp1","tp2","tp3","suggested_lots","risk_twd","reason_full","inst_signal"],
    "daily_report_time":   "16:45",
    "morning_brief_time":  "08:45",
    "max_signals_per_day": 10,
    "max_free_signals_per_day": 3,
}

FUBON_CONFIG = {
    "enabled": False, "paper_trading": True,
    "auto_trade": False, "market": "TSE",
    "order_type": "ROD", "price_type": "LMT",
}

SYSTEM = {
    "version":        "1.0.0",
    "name":           "台股波段智慧交易系統",
    "name_en":        "TW Stock Swing AI System",
    "timezone":       "Asia/Taipei",
    "web_port":       5000,
    "scan_batch_size": 50,
    # ★ 修正：2026-09-03——scanner.py 把單檔序列掃描改成同一批次內最多 4 檔
    # 並行後，這個值的意義從「每檔掃完後的序列延遲」變成「並行任務之間的
    # 啟動間隔」，數值需要跟著調降（原本 1.5 秒是序列執行時用來降低對外部
    # API 的請求頻率，並行模式下沿用同一數值只會讓整批任務啟動時間拉得
    # 更長，起不到原本的節流效果，也沒有縮短掃描時間的效果）。0.3 秒的
    # 啟動間隔搭配 4 檔並行，尖峰請求速率跟原本序列模式的量級相近，
    # 是保守但仍有感縮短總時間的折衷值；如果之後觀察到外部 API 出現更多
    # 429/逾時/憑證類錯誤，優先調高這個值，而不是調高並行數。
    "scan_delay_sec": 0.3,
    "cache_ttl_sec":  3600,
    "db_path":        "instance/twstock.db",
    "log_level":      "INFO",
}

DISCLAIMER = (
    "⚠️ 本訊號由 AI 技術分析自動生成，僅供學習參考，不構成投資建議。\n"
    "台股交易存在市場風險，請自行評估後再進行投資決策。盈虧自負。"
)

