"""
config.py — 台股波段智慧交易系統 v1.0
"""
import os
from datetime import datetime, timedelta
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
    # ★ 修正：2026-09-26——使用者要求把全市場掃描的流動性門檻從500張調低，
    # 讓更多股票被納入掃描範圍。用 /api/diagnostics/universe_thresholds
    # 實測目前全市場（上市+上櫃，扣掉股價≤5元）分布後，使用者選擇調到200張：
    # 500張→847檔，200張→1,163檔（多納入316檔，約+37%）。200張是這次列出的
    # 候選值裡，覆蓋率明顯提升、但均量門檻仍保留一定流動性緩衝的選擇（100張以下
    # 流動性風險開始明顯增加，可能放大零股買不到/賣不掉、滑價侵蝕分批出場獲利
    # 的問題——見稽核報告對分批出場49.1%勝率的討論）。
    "min_avg_volume": 200,
}
THRESH = SIGNAL_THRESHOLDS

# ★ 新增：2026-09-19——使用者要求詳細檢測「做空是否可行」，用 backtester.py
# 新增的 by_direction 統計跑了一次 TW50 成分股的真實回測（bug修完後的現行邏輯，
# min_score=65，約243根日線/1年）：buy方向160筆交易、勝率63.6%、總損益
# +909,135；sell方向29筆交易（涵蓋19/50檔，不是單一個股拖累）、勝率只有
# 14.8%、總損益 -128,348，平均每筆虧4,426元——多空兩個方向的表現有非常明顯
# 的系統性落差。這個結果還沒扣掉真實放空要付的融券利息/借券費（backtester.py
# 目前buy/sell用同一套手續費+證交稅公式，完全沒有模擬融券的額外成本），代表
# 實際放空的表現只會比這個回測結果更差，不會更好。另外要誠實說明：這個回測
# 用的1年歷史區間剛好涵蓋哪一段大盤走勢（多頭或整理）沒有另外查證，如果這段
# 期間大盤/這些權值股整體是偏多頭格局，逆勢放空本來就會系統性地吃虧，不能
# 100%排除是「回測區間剛好對空方不利」而不是「空方邏輯本身设计有問題」——
# 但不管是哪個原因，現階段的證據都指向「現在的做空訊號不該被當成跟做多訊號
# 同等可信」。在做空邏輯有專門的重新設計跟驗證之前，先暫停對外推播做空訊號，
# 只保留做多——這是可逆的開關，不是刪除做空功能，之後有把握了隨時可以改回
# True。
ENABLE_SHORT_SIGNALS = False

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
    "max_daily_signals": 10,
    # ★ 修正：2026-09-24——使用者反映實盤「常常停損、很少停利」，追查後用
    # /api/backtest/full 實跑 TW50 得到量化證據：做多勝出（tp1/tp2）交易平均
    # 要花 11.9 個交易日才觸價，其中 88.8% 的獲利交易是在超過 3 天之後才觸價
    # ——但 signal_expire_days 原本設 3，_resolve_pending_signals() 會在訊號
    # 產生滿 3 個交易日、還沒摸到停損/停利時，直接用當下市價強制平倉、標記
    # 「expired」。換句話說：backtest 拿來驗證策略勝率(59.4%)的邏輯，跟實盤
    # 真正在跑的持倉規則，兩者對「能不能等到停利」這件事的假設完全不一致，
    # 等於絕大多數backtest裡本來會贏的交易，實盤都還沒走到停利就被強制
    # 平倉、常常小虧收場（這正是使用者這幾週實際看到的4筆結算訊號：2 筆
    # sl + 2 筆 expired，4 戰全敗）。停損本身不受 expire_days 影響（sl 觸價
    # 是即時判斷，不用等到期限），所以延長 expire_days 不會放大單筆虧損上限，
    # 只是給真正會贏的訊號更多時間走到停利。改成 15 個交易日（約3週），
    # 用同一份回測資料估算可以讓約 74.5% 的獲利交易在到期前真正觸及停利
    # （3天只能接住 11.2%），是「多接住獲利」跟「不要放到無限久」之間的
    # 合理折衷；之後有更多樣本可以再依實際結算數字微調。
    "signal_expire_days": 15,
}
CB = CIRCUIT_BREAKER

CORRELATION_GROUPS = [
    ["半導體", "AI概念", "電子零組件"],
    ["金融保險"],
    ["航運"],
    ["生技醫療"],
    ["ETF"],
]
# ★ 修正：2026-09-16——稽核（含跟 Perplexity/ChatGPT/Gemini 三方交叉比對）發現
# CORRELATION_GROUPS 定義了之後從未被任何邏輯引用，scanner.py 的同產業去重只看
# sig["sector"] 這個字串本身（例如「半導體」「AI概念」「電子零組件」是三個不同
# 字串，各自都能通過去重），完全沒有把這三個高度連動的產業視為同一曝險籃子。
# 結果就是「最大持倉5檔」這個限制可能名義上分散在5個不同sector字串，實際上
# 全部是半導體供應鏈，一旦該族群系統性回檔，5檔會同步觸及停損。這裡把
# CORRELATION_GROUPS 真正接進 scanner.py _filter_and_rank()，同一相關性群組
# （不論組內哪個sector字串）最多同時持有這個數量的部位（含已在手上的未平倉
# 部位，不是只看當天新掃出來的候選）。
MAX_PER_CORRELATION_GROUP = 2

# ★ 新增：2026-09-16——回應稽核報告 B1（財報公布前後無風險過濾，risk_manager.py
# 的模組說明明確承認過去移除了 check_earnings_risk）。這是三方 AI 交叉比對
# （Perplexity/ChatGPT/Gemini）後排定的最高優先剩餘項目。
#
# 台灣上市櫃公司財報法定申報截止日是證交所/櫃買中心統一規定、對全市場公司
# 都適用的固定日期（已用 WebSearch 查證 2026 年版本）：第一季季報 5/15、
# 第二季（半年報）8/14、第三季季報 11/14，均為「季末後45日」；年度財報是
# 「年度終了後3個月」，一般公司 3/31 前，實收資本額100億以上或金融保險業則
# 提前到 3/15 前。這幾個日期不需要額外資料源就能100%準確判斷「現在是不是
# 財報密集公告期」。
#
# 真正做到「某一檔股票精確哪一天公布財報」需要另外串接公開資訊觀測站(MOPS)
# 的個股財報/重大訊息查詢，但那類 TWSE 端點在這個專案裡已經有過活生生的
# 404失效前例（見 stock_universe.py 處置股清單的註解），與其冒著再引入一個
# 不穩定資料源的風險去追求「精確到哪一檔」，這裡先用「全市場所有個股在同一
# 個密集期都提高警覺（門檻微幅提高 + Telegram明確提示）」的保守、可驗證做法；
# 之後如果要做到逐檔精確過濾，應該另外評估 MOPS 整合的可行性與穩定性。
EARNINGS_SEASON_DEADLINES = [(3, 31), (5, 15), (8, 14), (11, 14)]
EARNINGS_SEASON_LOOKBACK_DAYS = 14  # 涵蓋約10個交易日的密集公告期（截止日前最後衝刺最密集）


def is_earnings_season(dt=None):
    """回傳 dt（預設現在）是否落在上述任一財報申報截止日前 EARNINGS_SEASON_LOOKBACK_DAYS
    天內（含截止日當天）。回傳 {"in_season": bool, "deadline": "MM/DD"|None, "days_left": int|None}。
    只依賴固定日期計算，不需要任何外部資料源，年年適用、不會因為上游API失效而悄悄失效。"""
    dt = dt or datetime.now()
    today = dt.date()
    for month, day in EARNINGS_SEASON_DEADLINES:
        deadline = datetime(dt.year, month, day).date()
        window_start = deadline - timedelta(days=EARNINGS_SEASON_LOOKBACK_DAYS)
        if window_start <= today <= deadline:
            return {"in_season": True, "deadline": f"{month}/{day}", "days_left": (deadline - today).days}
    return {"in_season": False, "deadline": None, "days_left": None}


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
    "台股交易存在市場風險，請自行評估後再進行投資決策。盈虧自負。\n"
    # ★ 新增：2026-09-16——稽核（含跟 Perplexity/ChatGPT/Gemini 三方交叉比對）
    # 一致指出：本系統不自動下單、無券商端掛單，損益計算（calc_tw_pnl）只
    # 內含手續費與證交稅，未計入滑價與實際手動下單的執行落差；顯示的SL/TP
    # 是計算上的價位，不是券商保證會成交的價位。這裡把這個系統性限制明講
    # 出來，而不是讓使用者誤以為看到的數字就是保證能拿到的實際結果。
    "📌 本系統僅推播訊號、不自動下單，也沒有券商端停損單；"
    "績效統計以訊號價位計算，未計入手動下單的滑價與執行落差，實際結果可能與統計數字有落差。\n"
    # ★ 新增：2026-09-16——ChatGPT在三方交叉比對中指出的落差：TP1/TP2/TP3
    # 訊號訊息裡標示「各出1/3分批出場」，是建議給使用者參考的手動操作策略；
    # 但系統自己統計的勝率/損益（scanner.py _resolve_pending_signals()、
    # backtester.py），為了避免跟過去已經修過的「回測公式跟實盤不一致」
    # （A-4）同類型錯誤，刻意讓實盤結算跟回測用同一套「以最先觸及的價位
    # （停損或任一停利）計算整筆損益」簡化模型，並不是真的按1/3/1/3/1/3
    # 分批加權計算——這裡明講這個落差，避免使用者誤以為看到的勝率/損益數字
    # 已經反映了分批出場的效果。
    "📌 TP1/TP2/TP3「各出1/3」是建議的手動分批出場策略，"
    "但系統統計的勝率與損益是以最先觸及的單一價位（停損或任一停利）計算整筆損益，"
    "並未按分批比例加權，如果你採用分批出場，實際結果會跟統計數字不同。"
)

# ════════════════════════════════════════════════
# ★ 新增：2026-09-25——使用者反映「今天沒開盤」（9/25中秋節），稽核發現
# get_market_session()（data_fetcher.py）跟所有排程（job_daily_scan、
# job_intraday_check 等，app.py）都只檢查「是不是週末」（weekday()>=5），
# 完全沒考慮國定假日——平日遇到國定假日（像中秋節、端午節這種不一定固定
# 在週末的假日）系統會誤判成「開盤」，照常觸發全市場掃描/盤中安全網檢查，
# 浪費運算資源，還可能把假日當天抓到的（其實是前一交易日的）舊報價誤判成
# 「今天有成功取得現價」。這裡把台灣證交所2026年已公告的休市日整理成一個
# 明確的日期集合，給 get_market_session() 和排程共用查詢。
# 資料來源（2026-09-25查證，人事行政總處115年行事曆＋券商休市日曆交叉比對）：
# 元旦、春節（含封關後2/12-2/13不開盤～2/22，2/23開紅盤）、和平紀念日、
# 兒童節/清明節、勞動節、端午節、中秋節、教師節、國慶日、光復節、行憲紀念日。
# ★ 重要：這份清單需要每年手動更新（台股休市日期每年都由交易所另行公告，
# 尤其是否有颱風假/臨時停市等特殊情況無法事先預測）。如果程式執行時的年份
# 不在下面的清單裡，get_market_session() 會退回只用「週末」判斷並記警告
# log，而不是直接出錯——避免年份切換時系統整個誤判成「永遠休市」。
TW_MARKET_HOLIDAYS = {
    2026: {
        "2026-01-01",  # 中華民國開國紀念日
        "2026-02-12", "2026-02-13",  # 春節前，市場不開盤（封關日2/11為最後交易日）
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 除夕～春節～小年夜補假
        "2026-02-27",  # 和平紀念日
        "2026-04-03", "2026-04-06",  # 兒童節/清明節（4/3補假、4/6補假）
        "2026-05-01",  # 勞動節
        "2026-06-19",  # 端午節
        "2026-09-25",  # 中秋節
        "2026-09-28",  # 教師節
        "2026-10-09",  # 國慶日補假
        "2026-10-26",  # 臺灣光復節補假
        "2026-12-25",  # 行憲紀念日
    },
}

