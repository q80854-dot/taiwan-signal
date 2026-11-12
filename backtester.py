"""
backtester.py — 台股波段版 v1.0
修正：pnl 改台股股數計算（含手續費+證交稅）
移除：外匯 pip/lot 計算、外匯黑名單
"""
import logging, time, math, bisect
from datetime import datetime, timezone
from typing import Dict, List, Optional
from config import ACCOUNT_BALANCE_TWD, SIGNAL_THRESHOLDS as THRESH, COMMISSION_RATE, TAX_RATE_SELL, SHARES_PER_LOT, MIN_COMMISSION, CIRCUIT_BREAKER as CB, SHORT_SIGNAL_THRESH, SHORT_REQUIRE_WEEKLY_BEARISH

logger = logging.getLogger(__name__)

# ★ 新增：2026-09-24——使用者要求「用新增的東西（K線快取／總經權重／基本面硬性
# 過濾）做詳細回測，跟過去策略比較勝率有沒有提升」，後續並明確要求「用真實資料
# 回測，不要假數據，能補的都補上」。目前實際能不能被回測驗證：
#   1) K線持久化快取（ohlcv_bars）：純基礎設施，不改變任何訊號判斷邏輯，只是把
#      fetch_ohlcv() 的資料來源從「每次重新下載」換成「本地快取+增量更新」，回測
#      跟實盤用的是同一份 fetch_ohlcv()，所以這裡的改動對回測結果完全沒有影響
#      （唯一影響是回測跑起來變快）——不需要、也沒辦法用「勝率有沒有提升」來
#      驗證這塊，這裡不對它做任何回測。
#   2) 總經指標加權：backtester.backtest_symbol_tw() 呼叫的是
#      check_multi_timeframe_tw()，不是 generate_signal_tw()，原本完全繞過新邏輯。
#      use_macro_overlay 參數在回測迴圈裡「補」上這段調整，涵蓋兩項有真實歷史
#      資料可用的指標：
#        - 大盤(TWII)熔斷：用 yfinance ^TWII 真實歷史指數，見 _fetch_twii_change_map()。
#        - 融資餘額日增減：用 TWSE MI_MARGN 的歷史 date= 查詢（已用 WebFetch 實測
#          驗證過真的能查到過去日期的資料）回填的真實歷史，見
#          data_fetcher.backfill_margin_history() 跟 _fetch_margin_chg_map()。
#      外資買賣超這項仍然沒做：市場總計端點(BFI82U)實測完全不支援歷史日期查詢，
#      個股層級的T86雖然支援歷史查詢，但要換算成全市場買賣超金額需要逐股乘上
#      當天股價再加總（上千檔×幾百個交易日），這個專案自己的文件也記錄過T86本身
#      不穩定，成本/風險不成比例，這裡誠實地不做。
#   3) 個股基本面月營收硬性過濾：fetch_monthly_revenue_map()（TWSE OpenAPI）只有
#      當下快照、沒有歷史查詢參數（已實測），但另外查到 MOPS 有官方歷史月營收
#      彙總頁（t21sc03，台股量化圈公開常用的正式資料來源），可以回填真實歷史，
#      見 fundamentals.backfill_monthly_revenue_history()。use_fundamentals_filter
#      參數在回測迴圈裡，用「事後（point-in-time，考慮申報時間差、不作弊看未來
#      資料）」的方式套用這項硬性過濾，見 fundamentals.check_fundamental_hard_filter_asof()。
_twii_chg_cache: Dict = {}

def _fetch_twii_change_map() -> Dict[str, float]:
    """回傳 {日期字串: 當日大盤(TWII)漲跌%}，用來在回測裡重建「這一天，
    如果 check_market_circuit_breaker() 有被套用，會不會觸發」。用 fetch_ohlcv
    抓（跟系統其他地方抓K線走同一條路，也會吃到新的 ohlcv_bars 持久化快取），
    不是另外發明一套抓取邏輯。"""
    if _twii_chg_cache.get("map") is not None and time.time() - _twii_chg_cache.get("ts", 0) < 3600:
        return _twii_chg_cache["map"]
    try:
        from data_fetcher import fetch_ohlcv
        data = fetch_ohlcv("^TWII", "daily")
        if not data or not data.get("dates"):
            logger.warning("_fetch_twii_change_map: 抓不到 ^TWII 歷史資料，總經回測這部分會跳過（視同大盤正常）")
            return {}
        dates = data["dates"]; closes = data["closes"]
        chg_map = {}
        for i in range(1, len(closes)):
            if closes[i-1]:
                chg_map[dates[i]] = round((closes[i] - closes[i-1]) / closes[i-1] * 100, 2)
        _twii_chg_cache["map"] = chg_map
        _twii_chg_cache["ts"] = time.time()
        return chg_map
    except Exception as e:
        logger.warning(f"_fetch_twii_change_map: {e}")
        return {}

_margin_chg_cache: Dict = {}

def _fetch_margin_chg_map() -> Dict[str, float]:
    """回傳 {日期字串: 當日融資餘額漲跌%}，讀 data_fetcher.backfill_margin_history()
    回填進 state_store 的真實歷史（不是即時抓取——回測不該在迴圈裡對 TWSE
    發出幾百次即時請求，那是回填腳本的工作，回測這裡只負責查表）。"""
    if _margin_chg_cache.get("map") is not None and time.time() - _margin_chg_cache.get("ts", 0) < 3600:
        return _margin_chg_cache["map"]
    try:
        from state_store import store
        m = store.get_margin_chg_map()
        _margin_chg_cache["map"] = m
        _margin_chg_cache["ts"] = time.time()
        return m
    except Exception as e:
        logger.warning(f"_fetch_margin_chg_map: {e}")
        return {}

def _macro_score_adj(day_date: Optional[str], twii_chg_map: Dict[str, float],
                      margin_chg_map: Optional[Dict[str, float]] = None) -> int:
    """複製 signal_engine.generate_signal_tw() 裡那段 macro_adj 的邏輯：大盤(TWII)
    熔斷、融資餘額急縮這兩項有真實歷史資料可查的指標（外資買賣超見上方說明，
    沒有可用的歷史資料，不在這裡）。門檻跟扣分幅度直接沿用 risk_manager 裡
    check_market_circuit_breaker()／check_margin_change() 的真實規則(CB)，
    不是另外發明一套。margin_chg_map 沒有資料的日期（還沒回填到）視同無異常，
    不憑空扣分。"""
    adj = 0
    if day_date and day_date in twii_chg_map:
        if twii_chg_map[day_date] <= CB["twii_drop_caution"]:
            adj -= 10
    if day_date and margin_chg_map and day_date in margin_chg_map:
        if margin_chg_map[day_date] <= CB.get("margin_change_warning", -5.0):
            adj -= 8
    return adj

# ★ 新增：2026-09-28——item 3（walk-forward/OOS 驗證第二輪已上線的放空分層閘門，
# ChatGPT 明確建議「先驗證再動 alpha_score」）。backtest_symbol_tw() 原本完全沒有
# 套用 signal_engine.generate_signal_tw() 裡新增的 Layer1 Market Regime Gate／
# Layer2 ADX方向確認（adx_bias）——回測只呼叫 check_multi_timeframe_tw()，這兩層
# 閘門是 generate_signal_tw() 自己額外做的過濾，回測完全繞過了。這裡補上「歷史
# 逐日 TWII regime」重建（不是只算「現在這一天」再套用到整段歷史，那會是用未來
# 資料污染過去判斷的look-ahead bias），讓回測放空進場邏輯能跟實盤對齊。
# 大盤情緒(SHORT_MAX_SENTIMENT)這項次要保護因為系統沒有歷史逐日 sentiment_score
# 資料可回填，比照 _fetch_margin_chg_map() 「沒有歷史回填資料就誠實跳過，不假裝
# 一直通過」的既有原則，這裡不套用，跟 run_comparison_backtest_tw() 對外資買賣超
# 的處理方式一致。
_twii_regime_cache: Dict = {}

def _fetch_twii_regime_series() -> Dict[str, bool]:
    """回傳 {日期字串: bearish_regime bool}，判定邏輯跟 data_fetcher.
    fetch_market_regime() 完全一致（price<MA60 且 MA60的5日斜率<0 且 MA20<MA60），
    差別是這裡對 ^TWII 全部歷史「逐日」重算，每一天都只用「那天當下看得到」的
    價格窗口（跟 backtest_symbol_tw() 其他地方逐K棒重建 tf_data 一樣的紀律），
    不是拿最新一天算出來的單一 regime 套用到整段歷史。資料不足65根（60日MA+
    5日斜率回看）的日期不會出現在回傳的 map 裡，呼叫端要當成「不確定、不放行」
    處理（見 _short_gate_pass()）。"""
    if _twii_regime_cache.get("map") is not None and time.time() - _twii_regime_cache.get("ts", 0) < 3600:
        return _twii_regime_cache["map"]
    try:
        from data_fetcher import fetch_ohlcv
        data = fetch_ohlcv("^TWII", "daily")
        if not data or not data.get("dates"):
            logger.warning("_fetch_twii_regime_series: 抓不到 ^TWII 歷史資料，Market Regime Gate 這部分驗證會跳過")
            return {}
        dates = data["dates"]; closes = data["closes"]; n = len(closes)
        ma60_list = [None] * n
        run_sum = 0.0
        for i in range(n):
            run_sum += closes[i]
            if i >= 60:
                run_sum -= closes[i - 60]
            if i >= 59:
                ma60_list[i] = run_sum / 60
        regime_map = {}
        for i in range(n):
            if i < 64 or ma60_list[i] is None or ma60_list[i - 5] is None:
                continue
            ma60 = ma60_list[i]; ma60_5ago = ma60_list[i - 5]
            ma60_slope_pct = (ma60 - ma60_5ago) / ma60_5ago * 100 if ma60_5ago else 0.0
            ma20 = sum(closes[i - 19:i + 1]) / 20
            price = closes[i]
            regime_map[dates[i]] = bool(price < ma60 and ma60_slope_pct < 0 and ma20 < ma60)
        _twii_regime_cache["map"] = regime_map
        _twii_regime_cache["ts"] = time.time()
        logger.info(f"_fetch_twii_regime_series: 建好 {len(regime_map)} 個交易日的歷史大盤regime序列")
        return regime_map
    except Exception as e:
        logger.warning(f"_fetch_twii_regime_series: {e}")
        return {}


def _short_gate_pass(day_date: Optional[str], mtf: Dict, twii_regime_map: Dict[str, bool]) -> bool:
    """複製 signal_engine.generate_signal_tw() 放空邏輯的 Layer1（大盤regime，
    用上面的歷史逐日序列，不含 SHORT_MAX_SENTIMENT 單日情緒次要保護，理由見
    本區塊開頭說明）／Layer2（週線硬性偏空＋ADX方向確認 adx_bias）兩層硬性
    條件；Layer3（分數/ADX強度/量能門檻）在呼叫端沿用 SHORT_SIGNAL_THRESH。"""
    if not day_date or day_date not in twii_regime_map or not twii_regime_map[day_date]:
        return False
    if SHORT_REQUIRE_WEEKLY_BEARISH and "bearish" not in mtf.get("weekly_bias", "neutral"):
        return False
    if mtf.get("adx_bias", "neutral") != "bearish":
        return False
    return True


# ★ 新增：2026-09-29——item：涵蓋真實空頭期間。ChatGPT/Perplexity 對「新放空
# 分層閘門在近1年TW50樣本裡0筆交易」這個結果一致指出：無法區分「閘門在真正
# 空頭期間也一樣有效攔住爛訊號」還是「近1年（幾乎全程多頭regime）根本沒出現
# 過閘門會放行的空頭條件」，這個問題不解決，0筆交易的結論就撐不住嚴謹檢驗。
# production 的 fetch_ohlcv() daily 固定用 config.TIMEFRAMES["daily"]["period"]
# ="1y"（live 掃描本來就只需要近1年），回測沿用同一套 fetch_ohlcv()（見上方
# 多處說明：回測跟實盤要用同一份資料/邏輯，不能各吃一套），所以連帶把回測
# 樣本也綁死在近1年。這裡另開一條完全獨立的資料來源，直接呼叫 yfinance 抓
# 長期歷史（預設 period="max"，涵蓋2022年台股真實空頭），跟 production 的
# fetch_ohlcv()/state_store 持久化快取完全分開、互不寫入也互不讀取——不修改
# config.TIMEFRAMES、不動 live 掃描，純粹是這個驗證用途專屬的獨立抓取路徑，
# 用完即丟（只快取在這個模組內的記憶體字典，跟 _twii_regime_cache 等既有
# 作法一致）。
_long_hist_cache: Dict = {}

def _fetch_long_history_tw(ticker: str, tf_key: str, period: str = "max") -> Optional[Dict]:
    """回傳跟 data_fetcher.fetch_ohlcv() 同樣形狀（dates/opens/highs/lows/closes/
    volumes 平行陣列）的字典，但資料期間由呼叫端指定（預設 period="max"，
    盡量抓到該檔全部可得歷史），不受 config.TIMEFRAMES 的 1y/5y 上限限制。
    直接呼叫 yfinance＋複用 data_fetcher._yf_rows_to_bars() 做同一套K棒轉換
    （避免另外發明一套解析邏輯、跟 production 對同一份原始資料的解讀不一致），
    但不經過 state_store 持久化快取，只在本模組記憶體內快取（避免同一次
    驗證跑批時對同一檔重複發出 yfinance 請求）。"""
    cache_k = f"{ticker}|{tf_key}|{period}"
    if cache_k in _long_hist_cache:
        return _long_hist_cache[cache_k]
    try:
        import yfinance as yf
        from data_fetcher import _yf_rows_to_bars
        interval = "1wk" if tf_key == "weekly" else "1d"
        h = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        if h is None or h.empty:
            _long_hist_cache[cache_k] = None
            return None
        bars = _yf_rows_to_bars(h, tf_key)
        if not bars:
            _long_hist_cache[cache_k] = None
            return None
        result = {
            "closes":  [b["close"]  for b in bars],
            "opens":   [b["open"]   for b in bars],
            "highs":   [b["high"]   for b in bars],
            "lows":    [b["low"]    for b in bars],
            "volumes": [b["volume"] for b in bars],
            "dates":   [b["date"]   for b in bars],
        }
        _long_hist_cache[cache_k] = result
        return result
    except Exception as e:
        logger.warning(f"_fetch_long_history_tw {ticker}/{tf_key}: {e}")
        _long_hist_cache[cache_k] = None
        return None


def _truncate_ohlcv_to_date(data: Optional[Dict], date_end: Optional[str]) -> Optional[Dict]:
    """把 _fetch_long_history_tw() 抓回來的長期歷史，截到 date_end（含）為止——
    backtest_symbol_tw() 收盤前若持倉未平倉，會用陣列最後一筆價格強制平倉，
    若不截斷，驗證某個歷史區間（如2022年）時就會用區間結束之後的未來價格
    強制平倉，污染統計，所以驗證期間之外的資料必須先截掉，不能只靠
    date_start 過濾「進場」那一半。"""
    if not data or not date_end:
        return data
    dates = data.get("dates", [])
    idx = bisect.bisect_right(dates, date_end)
    if idx <= 0:
        return None
    return {k: v[:idx] for k, v in data.items()}


# ★ 修正：2026-08-30（第二輪）——跟 signal_engine.calc_position_size() 的修正配套：不再假設倉位
# 一定是「張」(1000股)的整數倍，改直接吃股數。calc_tw_pnl 的 shares 參數現在就是股數本身，
# 不用再乘 SHARES_PER_LOT。
# ★ 修正：2026-09-29（跨AI覆核發現，經 grep/Read 驗證屬實）——sell_tax 原本無條件對
# close 課證交稅，這在 direction="buy"（做多，賣出出場才是真正的賣出那一筆交易）
# 是對的；但 direction="sell"（做空）的真正賣出交易是 entry（放空進場那一筆），
# close 只是買回回補（買進），不該課證交稅。跟 signal_engine.calc_trade_cost() 的
# 正確作法（用「哪一筆才是真正的賣出交易」來課稅，不是無條件用 close）對齊。
# 目前 ENABLE_SHORT_SIGNALS=False，即時交易不受影響，但這個函式仍被回測/歷史
# 空單績效統計使用，稅基算錯會讓空單的回測 P&L 系統性偏低，影響「要不要重新
# 開放放空」這類決策所依據的數字。
def calc_tw_pnl(entry, close, direction, shares, days_held=0):
    # ★ 2026-10-09：納入滑價與放空借券費，貼近真實成交。買進成交略高、賣出略低（兩邊各 SLIPPAGE_RATE）；
    # 放空另依持有天數計入借券費。手續費／證交稅以「實際成交腳價」計算。
    from config import SLIPPAGE_RATE, SHORT_BORROW_ANNUAL
    if direction == "buy":
        eff_entry = entry * (1 + SLIPPAGE_RATE); eff_exit = close * (1 - SLIPPAGE_RATE)
        gross = (eff_exit - eff_entry) * shares
        buy_leg, sell_leg = eff_entry, eff_exit
    else:
        eff_entry = entry * (1 - SLIPPAGE_RATE); eff_exit = close * (1 + SLIPPAGE_RATE)   # 放空：賣出開倉偏低、回補偏高
        gross = (eff_entry - eff_exit) * shares
        buy_leg, sell_leg = eff_exit, eff_entry
    buy_fee  = max(MIN_COMMISSION, buy_leg * shares * COMMISSION_RATE)
    sell_fee = max(MIN_COMMISSION, sell_leg * shares * COMMISSION_RATE)
    sell_tax = sell_leg * shares * TAX_RATE_SELL
    borrow   = sell_leg * shares * SHORT_BORROW_ANNUAL * max(days_held, 0) / 365 if direction == "sell" else 0
    return round(gross - buy_fee - sell_fee - sell_tax - borrow, 0)

def _held_days(dates, bar_in, bar_now):
    """★ 2026-11-12（Q-P0-02）持有『日曆天』數，供放空借券費計算。原本各回測呼叫 calc_tw_pnl
    都沒傳 days_held（預設 0），放空的借券費恆為 0、淨損益被高估。有日期就用日期差；
    沒有日期序列時以交易日數 ×7/5 近似。"""
    try:
        return max(0, (datetime.strptime(str(dates[bar_now])[:10], "%Y-%m-%d")
                       - datetime.strptime(str(dates[bar_in])[:10], "%Y-%m-%d")).days)
    except Exception:
        try:
            return max(0, int(round((bar_now - bar_in) * 7 / 5)))
        except Exception:
            return 0


def _bt_entry_prices(closes, opens, i):
    """★ 2026-11-12 回測進場時序（與正式流程一致）：第 i 根的訊號只用 [:i]（到前一根為止）的資料，
    計畫價位（進場參考／停損／停利／部位大小）以『訊號日收盤』closes[i-1] 定錨，於第 i 根『開盤』
    成交。原本各回測以 closes[i]（隔日收盤）同時當計畫價與成交價——不是未來函數，但比正式流程
    晚一個交易時段，且進場當日的盤中行情完全不被檢查。回傳 (plan_price, fill_price) 或 (None, None)。"""
    try:
        plan = closes[i - 1] if i >= 1 else None
        fill = opens[i] if i < len(opens) else None
    except Exception:
        return None, None
    if not plan or plan <= 0 or not fill or fill <= 0:
        return None, None
    return plan, fill


def _bt_fill_valid(direction, fill, sl, tp1, bar=None, prev_close=None):
    """開盤價已穿越停損（風險無定義）或已越過 TP1（沒有報酬空間）＝計畫失效，不進場。
    給了 bar／prev_close 時另擋「進場當根一價鎖死」（做多鎖漲停買不到／放空鎖跌停空不到，Q-P0-07）。"""
    try:
        if bar is not None:
            import execution
            if execution.entry_blocked_by_limit(direction, bar, prev_close):
                return False
        return (sl < fill < tp1) if direction == "buy" else (tp1 < fill < sl)
    except Exception:
        return False


def _bt_bar(dates, opens, highs, lows, closes, i):
    return {"bar_date": dates[i] if i < len(dates) else "", "open": opens[i] if i < len(opens) else None,
            "high": highs[i], "low": lows[i], "close": closes[i]}


def _bt_exit_locked(direction, dates, opens, highs, lows, closes, i):
    """出場當根一價鎖死在不利方向（多單鎖跌停／空單鎖漲停）＝賣不掉／補不回，出場順延。"""
    import execution
    return i >= 1 and execution.exit_blocked_by_limit(direction, _bt_bar(dates, opens, highs, lows, closes, i), closes[i - 1])


def _bt_expire_days():
    """與正式訊號相同的到期窗（日曆天）。原本只有組合回測套用，單檔回測會一直抱到停利或停損，
    等於『無限持有到最終 TP』而高估勝率。"""
    return CB.get("signal_expire_days", 15)


def simulate_staged_exit(bars, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, expire_days=None,
                         prev_close=None):
    """★ 2026-11-08 Phase 1：分批出場判定統一由 execution.simulate_staged_exit 執行，與
    （未來的）其他回測路徑共用同一實作，避免漂移。成本仍走本模組的 calc_tw_pnl（以 pnl_fn
    依賴注入傳入），數字完全不變。保守規則與原本一致：TP1/TP2/TP3 各出 1/3、TP1→保本、
    TP2→移至 TP1、同 K 先停損、跳空以開盤成交、末段逾期以收盤結算。回傳結構與原本相同。"""
    import execution
    return execution.simulate_staged_exit(bars, entry, sl, tp1, tp2, tp3, direction, shares,
                                          gen_date, pnl_fn=calc_tw_pnl, expire_days=expire_days,
                                          prev_close=prev_close)


def backtest_symbol_tw(ticker, initial_balance=None, min_score=None, use_macro_overlay=False, use_fundamentals_filter=False, disabled_factors=None, use_short_gates=True,
                        daily_override=None, weekly_override=None, date_start=None) -> Dict:
    """
    ★ 新增：2026-09-29——daily_override/weekly_override/date_start 三個參數，
    給「涵蓋真實空頭期間」驗證用（見 _fetch_long_history_tw()）：daily_override／
    weekly_override 給定時，直接使用該資料而不呼叫 fetch_ohlcv()（用長期歷史
    取代 production 綁死的 1y/5y 視窗，資料格式必須跟 fetch_ohlcv() 回傳一致）；
    date_start 給定時，只是把「新開倉」限制在 date_start（含）之後——不影響
    已持倉部位的停損停利判斷、也不影響 date_start 之前資料仍被拿來算指標
    （這正是保留這段資料的目的：LOOKBACK=130 根日K的暖身期要在 date_start
    之前就先跑完，不能讓 date_start 當天才開始只有0根歷史可看）。三個參數
    都不給時，行為與修改前完全一致（None 時 daily_override/weekly_override
    直接 or 掉、date_start 判斷式直接短路為 False）。
    ★ 修正：改為直接呼叫 signal_engine 的 check_multi_timeframe_tw() / calc_stop_loss_tw() /
    calc_take_profits_tw() / calc_position_size()，跟 scanner.py 每天盤後真正在跑的邏輯用同一套，
    不再是 scoring_engine.calc_composite_score() 那套只有回測在用、實盤從未呼叫過的獨立評分法。
    ★ 修正：2026-09-19——原本這裡逐根K棒重建的 tf_data 只有 "daily"，沒有同步重建歷史上每一根
    K棒當下的週線資料，所以回測出來的是策略的「日線骨架」表現，週線確認趨勢那段加分/降分（尤其是
    「週線偏空/偏多、逆勢扣分」那段，見 signal_engine.check_multi_timeframe_tw）在回測裡完全不會
    生效，但實盤每天真的有套用。稽核 2026-09-19 的TW50回測結果時發現這是解讀「做空為什麼只有14.8%
    勝率」的一個重要混淆因子：79%的做空交易是真的觸發停損（不是樣本尾端強制平倉的假象），代表
    很多做空進場點本身就是在對抗當下更大格局的多頭趨勢——而這正是「週線確認」設計出來要擋掉的
    情況，但回測沒有真的套用這一層過濾，所以回測結果可能比「週線確認有生效」的真實情況更悲觀。
    這裡改成額外抓一次真正的週線歷史（fetch_ohlcv weekly，跟實盤 TIMEFRAMES 設定一樣抓5年），
    用日期對齊：每根日K往回看，只納入「週線起始日 <= 這根日K日期」的週線資料，模擬「當下那一天，
    系統看得到的週線資料範圍」，讓回測跟實盤用的是同一套多時框判斷，不再是只有日線的簡化版。
    小時線因為 yfinance 只提供約90天的小時資料、回測用的1年日線窗口大半時間根本抓不到對應的
    小時歷史，這裡先不處理，維持原本「回測不含小時共振」的已知限制。
    """
    from data_fetcher   import fetch_ohlcv
    from signal_engine  import check_multi_timeframe_tw, calc_stop_loss_tw, calc_take_profits_tw, calc_position_size
    from scoring_engine import calc_performance_metrics
    from state_store    import store
    min_score = min_score if min_score is not None else THRESH["min_score"]
    balance=initial_balance or ACCOUNT_BALANCE_TWD
    data=daily_override if daily_override is not None else fetch_ohlcv(ticker,"daily")
    if not data or len(data.get("closes",[]))<60:
        return {"error":f"{ticker} 歷史數據不足（需60根日線）"}
    closes=data["closes"]; highs=data["highs"]; lows=data["lows"]; opens=data["opens"]; volumes=data["volumes"]
    dates=data.get("dates",[])
    weekly_raw=weekly_override if weekly_override is not None else fetch_ohlcv(ticker,"weekly")
    w_dates  =weekly_raw.get("dates",[])   if weekly_raw else []
    w_closes =weekly_raw.get("closes",[])  if weekly_raw else []
    w_highs  =weekly_raw.get("highs",[])   if weekly_raw else []
    w_lows   =weekly_raw.get("lows",[])    if weekly_raw else []
    w_opens  =weekly_raw.get("opens",[])   if weekly_raw else []
    w_volumes=weekly_raw.get("volumes",[]) if weekly_raw else []
    def _weekly_asof(day_date):
        """回傳「這根日K當下」看得到的週線切片。
        ★ 修正：2026-09-29——稽核發現look-ahead bias（ChatGPT/Perplexity第二輪
        交叉檢查揪出的P0問題）：原本用「週線起始日<=day_date」取到的最後一根
        週線K棒，很可能是day_date當天所屬、還沒走完的「本週」——yfinance的
        週線K棒是用週一(起始日)標記，但這根K棒的OHLC是整週（一路到週五）的
        資料。如果day_date是週二～週五，代表這一週還沒收完，原本的判斷卻已經
        把「整週（含當天之後、還沒發生的幾天）」的高低收都當成「day_date當下
        已知」餵給模型，是真實存在的look-ahead bias，不是「理論上可能」而已。
        修正邏輯：w_dates[j+1]<=day_date時，週j才算「已經走完、當下真的看得到」
        （下一週都已經開始，代表這一週的交易日全部結束了）。等價寫法是
        bisect_right(w_dates, day_date)再往前收縮一格，把「day_date當天所屬、
        可能還在進行中」的那一根週線K棒整根排除，只用「已經完整結束」的週線
        歷史，不再假設「起始日<=day_date」就等於「這週已經走完」。"""
        if not w_dates or not day_date: return None
        w_idx=bisect.bisect_right(w_dates, day_date)-1
        if w_idx<20: return None
        return {"closes":w_closes[:w_idx],"highs":w_highs[:w_idx],"lows":w_lows[:w_idx],
                "opens":w_opens[:w_idx],"volumes":w_volumes[:w_idx]}
    try:
        from stock_universe import get_stock_info
        info=get_stock_info(ticker); size_cat=info.get("size_cat","中型股") if info else "中型股"
    except Exception:
        size_cat="中型股"
    n=len(closes); LOOKBACK=130; trades=[]; equity=[balance]; open_trade=None
    twii_chg_map=_fetch_twii_change_map() if use_macro_overlay else {}
    margin_chg_map=_fetch_margin_chg_map() if use_macro_overlay else {}
    twii_regime_map=_fetch_twii_regime_series() if use_short_gates else {}
    fund_code=ticker.split(".")[0] if use_fundamentals_filter else None
    if use_fundamentals_filter:
        from fundamentals import check_fundamental_hard_filter_asof
    logger.info(f"[BT] {ticker} 開始回測，共 {n} 根日線，size_cat={size_cat}，"
                f"use_macro_overlay={use_macro_overlay}，use_fundamentals_filter={use_fundamentals_filter}，"
                f"use_short_gates={use_short_gates}")
    # ★ 修正：2026-08-30——投資人報告核對數字時發現 Sharpe/最大回撤嚴重失真的根因：equity_curve
    #   原本只在「每次平倉當下」才記一筆，不是每根K棒都記。calc_performance_metrics() 卻把這條
    #   equity_curve 相鄰兩點的報酬率當「逐日報酬」處理、年化時乘上 sqrt(252)——但兩個平倉點之間
    #   往往隔了好幾天甚至好幾週，實際上是「逐筆交易報酬」被誤當成「逐日報酬」年化，交易筆數越少
    #   （min_score≥65 門檻本來就選得嚴）灌水越誇張，這是 Sharpe 動輒破百的直接原因；同時最大回撤
    #   也因此只看得到「已實現」損益的高低點，完全看不到持倉中途尚未停損/停利、帳面上一度虧很多的
    #   情況。改成不管有沒有平倉，「每一根K棒」都用當下收盤價把未平倉部位的浮動損益 mark-to-market
    #   計入 equity，讓 equity_curve 變成真正的逐日序列，calc_performance_metrics() 不用改，
    #   sqrt(252) 年化、回撤計算就都會是對的。
    EXPIRE_DAYS=_bt_expire_days()
    def _exit_check(i):
        """單次出場判定（停損優先；TP2 優先於 TP1）＋到期窗。回傳是否已出場。進場當日也會呼叫。"""
        nonlocal open_trade, balance
        d=open_trade["direction"]; sl=open_trade["sl"]; tp1=open_trade["tp1"]; tp2=open_trade["tp2"]
        if _bt_exit_locked(d,dates,opens,highs,lows,closes,i): return False
        hit_sl=(d=="buy" and lows[i]<=sl) or (d=="sell" and highs[i]>=sl)
        hit_tp2=(d=="buy" and highs[i]>=tp2) or (d=="sell" and lows[i]<=tp2)
        hit_tp1=(d=="buy" and highs[i]>=tp1) or (d=="sell" and lows[i]<=tp1)
        if hit_sl:
            res,cp="sl",sl
            op=opens[i] if i<len(opens) else None
            if op and ((d=="buy" and op<sl) or (d=="sell" and op>sl)): cp=op   # 跳空穿越停損：以開盤價成交
        elif hit_tp2: res,cp="tp2",tp2
        elif hit_tp1: res,cp="tp1",tp1
        elif _held_days(dates,open_trade["sig_bar"],i)>=EXPIRE_DAYS: res,cp="expired",closes[i]
        else: return False
        shares=open_trade.get("shares",1)
        pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,shares,days_held=_held_days(dates,open_trade["sig_bar"],i))
        entry_balance=balance
        balance+=pnl
        trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                        "sl":sl,"tp1":tp1,"result":res,"pnl_twd":pnl,"shares":shares,
                        "pnl_pct":round(pnl/entry_balance*100,2),"score":open_trade.get("score",0),
                        "bar_in":open_trade["bar"],"bar_out":i,"hold_days":i-open_trade["bar"]})
        open_trade=None
        return True
    for i in range(LOOKBACK,n):
        wd={"closes":closes[:i],"highs":highs[:i],"lows":lows[:i],"opens":opens[:i],"volumes":volumes[:i]}
        day_date=dates[i] if i<len(dates) else None
        price=closes[i]
        plan_px,fill_px=_bt_entry_prices(closes,opens,i)
        just_exited=_exit_check(i) if open_trade else False
        if open_trade is None and not just_exited and plan_px is not None and not (date_start and day_date and day_date<date_start):
            tf_data_bt={"daily":wd}
            w_slice=_weekly_asof(day_date)
            if w_slice: tf_data_bt["weekly"]=w_slice
            mtf=check_multi_timeframe_tw(tf_data_bt, disabled_factors=disabled_factors)
            direction=mtf.get("direction","none")
            fund_blocked=False
            if direction!="none" and use_fundamentals_filter and day_date:
                fund_chk=check_fundamental_hard_filter_asof(fund_code, day_date)
                fund_blocked=fund_chk.get("blocked", False)
            # ★ 新增：2026-09-28——item 3：放空這條路徑套用跟實盤 generate_signal_tw()
            # 一致的 Layer1(大盤regime)/Layer2(週線硬性偏空+ADX方向確認) 硬性閘門，
            # 見上方 _short_gate_pass() 說明。use_short_gates=False 時完全不套用，
            # 保留成「舊邏輯」基準，讓 run_short_gate_validation_tw() 可以做前後比較。
            short_gate_blocked=False
            if direction=="sell" and use_short_gates:
                short_gate_blocked=not _short_gate_pass(day_date, mtf, twii_regime_map)
            if direction!="none" and not fund_blocked and not short_gate_blocked:
                score=mtf.get("score",0)
                if use_macro_overlay:
                    macro_adj=_macro_score_adj(day_date, twii_chg_map, margin_chg_map)
                    if macro_adj:
                        score=max(0,score+macro_adj)
                # 放空且 Layer1/2 閘門已上線時，Layer3 訊號強度門檻比照實盤改用
                # SHORT_SIGNAL_THRESH（分數75/ADX25/量比1.5，都比做多門檻更嚴）。
                use_short_thresh = (direction=="sell" and use_short_gates)
                min_score_eff = SHORT_SIGNAL_THRESH["min_score"] if use_short_thresh else min_score
                min_adx_eff = SHORT_SIGNAL_THRESH["min_adx"] if use_short_thresh else THRESH["min_adx"]
                min_vol_eff = SHORT_SIGNAL_THRESH["min_vol_ratio"] if use_short_thresh else THRESH["min_vol_ratio"]
                if score>=min_score_eff:
                    adx_val=mtf.get("adx_value",0)
                    if adx_val>=min_adx_eff:
                        vol_ratio=mtf.get("vol_ratio",1.0)
                        if not(vol_ratio<min_vol_eff and score<75):
                            daily_ind=mtf.get("entry_indicators",{})
                            atr=daily_ind.get("atr",{}).get("value",0) or plan_px*0.02
                            if atr:
                                low_5d=min(lows[max(0,i-5):i]) if i>=5 else None
                                high_5d=max(highs[max(0,i-5):i]) if i>=5 else None
                                sl=calc_stop_loss_tw(direction,plan_px,atr,daily_ind,size_cat,low_5d,high_5d)
                                tp_info=calc_take_profits_tw(direction,plan_px,sl,size_cat)
                                # ★ 修正：2026-09-26——稽核發現回測跟實盤在進場過濾條件上有落差：
                                # signal_engine.generate_signal_tw() 有「停損距離>12%就跳過（追高/
                                # 已離結構性支撐太遠）」的過濾（見該檔案該函式），回測這裡完全沒有
                                # 套用，導致回測會納入實盤根本不會進場的訊號，讓回測結果失真。這裡
                                # 補上同一條件，跟實盤邏輯一致。同時 calc_position_size() 補上
                                # avg_volume_lots，跟實盤一樣用該股均量做流動性上限，不然回測部位
                                # 大小可能比實盤實際可成交的量還大。
                                sl_dist_pct=abs(plan_px-sl)/plan_px*100 if plan_px else 0
                                if tp_info["rr1"]>=THRESH["min_rr"] and sl_dist_pct<=12:
                                    pos=calc_position_size(plan_px,sl,balance=balance,size_cat=size_cat,
                                                            avg_volume_lots=(info.get("volume_lots") if info else None))
                                    if pos["shares"]>0 and _bt_fill_valid(direction,fill_px,sl,tp_info["tp1"],_bt_bar(dates,opens,highs,lows,closes,i),plan_px):
                                        open_trade={"direction":direction,"fill_price":fill_px,"sl":sl,"tp1":tp_info["tp1"],"tp2":tp_info["tp2"],
                                                    "score":score,"shares":pos["shares"],"bar":i,"sig_bar":i-1}
                                        _exit_check(i)          # 進場當日即納入出場判定
        if open_trade:
            unreal_pnl=calc_tw_pnl(open_trade["fill_price"],price,open_trade["direction"],open_trade.get("shares",1),days_held=_held_days(dates,open_trade["bar"],i))
            equity.append(balance+unreal_pnl)
        else:
            equity.append(balance)
    if open_trade:
        # ★ 修正：這裡不再額外 append 一筆 equity——上面逐K棒 mark-to-market 迴圈跑到最後一根
        # （i=n-1，price=closes[-1]）時，未平倉部位已經用同一個 cp=closes[-1] 算過浮動損益、
        # append 進 equity_curve 了，這裡只是把它「實現」成真正的 balance/trades 紀錄，數值一致，
        # 不需要重複記錄，否則 equity_curve 最後會多一個重複點。
        d=open_trade["direction"]; cp=closes[-1]; shares=open_trade.get("shares",1)
        pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,shares,days_held=_held_days(dates,open_trade["sig_bar"],n-1))
        balance+=pnl
        trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                        "result":"forced_close","pnl_twd":pnl,"shares":shares,"pnl_pct":round(pnl/balance*100,2),
                        "score":open_trade["score"],"bar_in":open_trade["bar"],"bar_out":n-1,"hold_days":n-1-open_trade["bar"]})
    metrics=calc_performance_metrics(equity,trades)
    # ★ 2026-11-12：到期出場（expired）依淨損益歸類勝負——原本只認 tp1/tp2 為勝、sl 為負，
    # 加入到期窗後若不歸類，到期單會從勝率分母消失。forced_close（資料結束強平）維持不計入。
    def _is_win(t): return t["result"] in ("tp1","tp2") or (t["result"]=="expired" and t["pnl_twd"]>0)
    def _is_loss(t): return t["result"]=="sl" or (t["result"]=="expired" and t["pnl_twd"]<=0)
    wins=[t for t in trades if _is_win(t)]
    losses=[t for t in trades if _is_loss(t)]
    init_bal=initial_balance or ACCOUNT_BALANCE_TWD
    # ★ 新增：2026-09-19——使用者反映策略不只做多、放空也是策略的一部分，要求
    # 詳細檢測放空這邊是否可行。原本這裡只回傳「不分方向」混在一起的勝率/損益，
    # 沒辦法回答「做多跟做空分開看，表現是不是有系統性差異」這個問題。這裡在
    # truncate成trades[-30:]之前，先用完整的trades（不是截斷後的30筆）分別
    # 算一份buy/sell各自的勝率、筆數、損益，讓run_full_backtest_tw()可以在
    # 多檔加總時，把兩個方向的統計分開呈現，而不是混在一起看不出方向性的差異。
    def _dir_stats(direction):
        d_trades=[t for t in trades if t["direction"]==direction]
        d_wins=[t for t in d_trades if _is_win(t)]
        d_losses=[t for t in d_trades if _is_loss(t)]
        return {"n_trades":len(d_trades),"n_wins":len(d_wins),"n_losses":len(d_losses),
                "win_rate":round(len(d_wins)/max(len(d_wins)+len(d_losses),1)*100,1),
                "total_pnl_twd":round(sum(t["pnl_twd"] for t in d_trades),0),
                "avg_pnl_twd":round(sum(t["pnl_twd"] for t in d_trades)/max(len(d_trades),1),0)}
    by_direction={"buy":_dir_stats("buy"),"sell":_dir_stats("sell")}
    return {"ticker":ticker,"n_bars":n,"n_trades":len(trades),"n_wins":len(wins),"n_losses":len(losses),
            "win_rate":round(len(wins)/max(len(wins)+len(losses),1)*100,1),
            "total_pnl_twd":round(sum(t["pnl_twd"] for t in trades),0),
            "initial_balance":init_bal,"final_balance":round(balance,0),
            "return_pct":round((balance-init_bal)/init_bal*100,2),
            "avg_hold_days":round(sum(t.get("hold_days",0) for t in trades)/max(len(trades),1),1),
            "sharpe":metrics.get("sharpe",0),"max_drawdown":metrics.get("max_drawdown",0),
            "calmar":metrics.get("calmar",0),"annual_return":metrics.get("annual_return",0),
            "max_consec_loss":metrics.get("max_consec_loss",0),
            "by_direction":by_direction,
            "equity_curve":equity,"trades":trades[-30:],"min_score_used":min_score,
            "grade":metrics.get("sharpe_grade","—"),"dd_grade":metrics.get("dd_grade","—"),
            "use_macro_overlay":use_macro_overlay,"use_fundamentals_filter":use_fundamentals_filter,
            "use_short_gates":use_short_gates,
            "entry_model":"訊號日收盤定錨計畫價位、隔日開盤成交","expire_days":EXPIRE_DAYS,
            "exit_model":"single（停損／TP2／TP1 擇一全出；非正式的分批出場，分批版見 backtest_symbol_tw_partial）",
            "universe_pit":"PIT_UNAVAILABLE",
            "completed_at":datetime.now(timezone.utc).isoformat()}

def walk_forward_backtest_tw(ticker, train_bars=150, test_bars=30, min_score=None) -> Dict:
    """★ 修正：同 backtest_symbol_tw，改用 signal_engine 的實盤邏輯，不再是 scoring_engine 的獨立評分。"""
    from data_fetcher   import fetch_ohlcv
    from signal_engine  import check_multi_timeframe_tw, calc_stop_loss_tw, calc_take_profits_tw, calc_position_size
    min_score = min_score if min_score is not None else THRESH["min_score"]
    data=fetch_ohlcv(ticker,"daily")
    if not data or len(data.get("closes",[]))<train_bars+test_bars:
        return {"error":"數據不足以進行 Walk-Forward"}
    closes=data["closes"]; highs=data["highs"]; lows=data["lows"]; opens=data["opens"]; volumes=data["volumes"]
    dates=data.get("dates",[])
    try:
        from stock_universe import get_stock_info
        info=get_stock_info(ticker); size_cat=info.get("size_cat","中型股") if info else "中型股"
    except Exception:
        size_cat="中型股"
    EXPIRE_DAYS=_bt_expire_days()
    n=len(closes); windows=[]; start=train_bars
    while start+test_bars<=n: windows.append((start-train_bars,start,start+test_bars)); start+=test_bars
    all_test_trades=[]; window_results=[]
    for win_idx,(tr_start,tr_end,te_end) in enumerate(windows):
        test_trades=[]; open_trade=None; balance=ACCOUNT_BALANCE_TWD
        def _wf_exit(i):
            """停損優先的單次出場（TP1）＋到期窗；進場當日也會呼叫。回傳是否已出場。"""
            nonlocal open_trade, balance
            d=open_trade["direction"]; sl=open_trade["sl"]; tp1=open_trade["tp1"]
            if _bt_exit_locked(d,dates,opens,highs,lows,closes,i): return False
            hit_sl=(d=="buy" and lows[i]<=sl) or (d=="sell" and highs[i]>=sl)
            hit_tp1=(d=="buy" and highs[i]>=tp1) or (d=="sell" and lows[i]<=tp1)
            if hit_sl:
                res,cp="sl",sl
                if opens[i] and ((d=="buy" and opens[i]<sl) or (d=="sell" and opens[i]>sl)): cp=opens[i]
            elif hit_tp1: res,cp="tp1",tp1
            elif _held_days(dates,open_trade["sig_bar"],i)>=EXPIRE_DAYS: res,cp="expired",closes[i]
            else: return False
            pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,open_trade.get("shares",1),days_held=_held_days(dates,open_trade["sig_bar"],i))
            balance+=pnl; t={"result":res,"pnl_twd":pnl,"score":open_trade["score"]}
            test_trades.append(t); all_test_trades.append(t); open_trade=None
            return True
        for i in range(tr_end,te_end):
            wd={"closes":closes[tr_start:i],"highs":highs[tr_start:i],"lows":lows[tr_start:i],"opens":opens[tr_start:i],"volumes":volumes[tr_start:i]}
            if len(wd["closes"])<130: continue
            price=closes[i]
            plan_px,fill_px=_bt_entry_prices(closes,opens,i)
            if open_trade and _wf_exit(i): continue
            if open_trade or plan_px is None: continue
            mtf=check_multi_timeframe_tw({"daily":wd})
            direction=mtf.get("direction","none")
            if direction=="none": continue
            score=mtf.get("score",0)
            if score<min_score: continue
            if mtf.get("adx_value",0)<THRESH["min_adx"]: continue
            daily_ind=mtf.get("entry_indicators",{})
            atr=daily_ind.get("atr",{}).get("value",0) or plan_px*0.02
            if not atr: continue
            local_i=i-tr_start
            low_5d=min(lows[max(tr_start,i-5):i]) if local_i>=5 else None
            high_5d=max(highs[max(tr_start,i-5):i]) if local_i>=5 else None
            sl=calc_stop_loss_tw(direction,plan_px,atr,daily_ind,size_cat,low_5d,high_5d)
            tp_info=calc_take_profits_tw(direction,plan_px,sl,size_cat)
            if tp_info["rr1"]<THRESH["min_rr"]: continue
            # ★ 修正：2026-09-26——同 backtest_symbol_tw() 補上的兩項，跟實盤 generate_signal_tw()
            # 對齊：停損距離>12%（追高過濾）就跳過；calc_position_size() 補上 avg_volume_lots
            # 流動性上限。
            sl_dist_pct=abs(plan_px-sl)/plan_px*100 if plan_px else 0
            if sl_dist_pct>12: continue
            pos=calc_position_size(plan_px,sl,balance=balance,size_cat=size_cat,
                                    avg_volume_lots=(info.get("volume_lots") if info else None))
            if pos["shares"]<=0: continue
            if not _bt_fill_valid(direction,fill_px,sl,tp_info["tp1"],_bt_bar(dates,opens,highs,lows,closes,i),plan_px): continue
            open_trade={"direction":direction,"fill_price":fill_px,"sl":sl,"tp1":tp_info["tp1"],"score":score,"shares":pos["shares"],"bar":i,"sig_bar":i-1}
            _wf_exit(i)                 # 進場當日即納入出場判定
        # ★ 修正：2026-11-04（F-12）——視窗末端仍未平倉的部位，原本直接被丟棄（不結算、
        # 不計入 test_trades/all_test_trades），導致每個視窗最後一筆交易的損益與勝負
        # 完全漏計、回測數字失真。本系統 walk-forward 是「獨立視窗測試」（每個視窗
        # 開頭都重設 balance），因此在視窗結束時以該視窗最後一根收盤價強制平倉並計入，
        # 結果標記為 "eow"（end-of-window）。
        if open_trade:
            cp=closes[te_end-1]
            pnl=calc_tw_pnl(open_trade["fill_price"],cp,open_trade["direction"],open_trade.get("shares",1),days_held=_held_days(dates,open_trade["sig_bar"],te_end-1))
            balance+=pnl; t={"result":"eow","pnl_twd":pnl,"score":open_trade["score"]}
            test_trades.append(t); all_test_trades.append(t); open_trade=None
        # 勝率改以「淨損益>0」計（對既有 tp1/sl 等價：tp1→正、sl→負；另可正確涵蓋 eow 強制平倉）
        wins_w=len([t for t in test_trades if t["pnl_twd"]>0])
        window_results.append({"window":win_idx+1,"n_trades":len(test_trades),
                                "win_rate":round(wins_w/max(len(test_trades),1)*100,1),
                                "total_pnl":round(sum(t["pnl_twd"] for t in test_trades),0)})
    all_wins=len([t for t in all_test_trades if t["pnl_twd"]>0])
    return {"ticker":ticker,"method":"walk_forward","n_windows":len(windows),"train_bars":train_bars,"test_bars":test_bars,
            "total_trades":len(all_test_trades),"overall_win_rate":round(all_wins/max(len(all_test_trades),1)*100,1),
            "total_pnl_twd":round(sum(t["pnl_twd"] for t in all_test_trades),0),
            "window_results":window_results,
            "stability":round(sum(1 for w in window_results if w["win_rate"]>=50)/max(len(window_results),1)*100,1),
            "completed_at":datetime.now(timezone.utc).isoformat()}

def run_full_backtest_tw(tickers=None, min_score=65.0, progress_cb=None, use_macro_overlay=False, use_fundamentals_filter=False, disabled_factors=None, use_short_gates=True) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components(); results=[]
    logger.info(f"[BT] 批量回測 {len(targets)} 檔，use_macro_overlay={use_macro_overlay}，use_fundamentals_filter={use_fundamentals_filter}，disabled_factors={disabled_factors}，use_short_gates={use_short_gates}")
    for idx,ticker in enumerate(targets):
        try:
            r=backtest_symbol_tw(ticker,min_score=min_score,use_macro_overlay=use_macro_overlay,use_fundamentals_filter=use_fundamentals_filter,disabled_factors=disabled_factors,use_short_gates=use_short_gates)
            if "error" not in r: results.append(r)
            # ★ 新增：2026-09-19——這個函式現在會被 app.py 包成背景執行緒跑（50檔
            # 全部跑完可能要幾分鐘），加一個可選的進度回呼，讓外層可以把「跑到第
            # 幾檔」寫回 state_store，之後才能做一個查詢進度的API，而不是完全黑箱、
            # 使用者只能等一個不知道要多久的結果。
            if progress_cb:
                try: progress_cb(idx+1, len(targets), ticker)
                except Exception: pass
            time.sleep(1.0)
        except Exception as e: logger.error(f"[BT] {ticker} 失敗: {e}")
    results.sort(key=lambda x:x.get("sharpe",0),reverse=True)
    # ★ 新增：2026-09-19——使用者要求詳細檢測「做空是否可行」，不能只看不分方向
    # 混在一起的勝率。這裡把所有檔位的 by_direction（見 backtest_symbol_tw 修正）
    # 加總成一份跨50檔的buy/sell總體統計，直接回答「多空兩個方向，統計上表現
    # 是否有系統性差異」這個問題。
    agg={"buy":{"n_trades":0,"n_wins":0,"n_losses":0,"total_pnl_twd":0},
         "sell":{"n_trades":0,"n_wins":0,"n_losses":0,"total_pnl_twd":0}}
    for r in results:
        bd=r.get("by_direction",{})
        for d in ("buy","sell"):
            s=bd.get(d,{})
            agg[d]["n_trades"]+=s.get("n_trades",0)
            agg[d]["n_wins"]+=s.get("n_wins",0)
            agg[d]["n_losses"]+=s.get("n_losses",0)
            agg[d]["total_pnl_twd"]+=s.get("total_pnl_twd",0)
    for d in ("buy","sell"):
        wl=agg[d]["n_wins"]+agg[d]["n_losses"]
        agg[d]["win_rate"]=round(agg[d]["n_wins"]/max(wl,1)*100,1)
        agg[d]["avg_pnl_twd"]=round(agg[d]["total_pnl_twd"]/max(agg[d]["n_trades"],1),0)
    n_trades_total=sum(r.get("n_trades",0) for r in results)
    n_wins_total=sum(r.get("n_wins",0) for r in results)
    overall_win_rate=round(n_wins_total/max(n_wins_total+sum(r.get("n_losses",0) for r in results),1)*100,1)
    avg_sharpe=round(sum(r.get("sharpe",0) for r in results)/max(len(results),1),2)
    avg_return_pct=round(sum(r.get("return_pct",0) for r in results)/max(len(results),1),2)
    return {"total":len(results),"completed_at":datetime.now(timezone.utc).isoformat(),
            "use_macro_overlay":use_macro_overlay,"use_fundamentals_filter":use_fundamentals_filter,
            "overall_win_rate":overall_win_rate,"n_trades_total":n_trades_total,
            "avg_sharpe":avg_sharpe,"avg_return_pct":avg_return_pct,
            "by_direction_aggregate":agg,
            "leaderboard":[{"ticker":r["ticker"],"win_rate":r["win_rate"],"sharpe":r["sharpe"],
                            "max_drawdown":r["max_drawdown"],"total_pnl_twd":r["total_pnl_twd"],
                            "return_pct":r["return_pct"],"grade":r["grade"],"n_trades":r["n_trades"],
                            "by_direction":r.get("by_direction",{})} for r in results],"details":results}


# ★ 新增：2026-11-04——組合級回測（使用者選定 5 項功能之一）。
# run_full_backtest_tw 是「每檔各自用一個獨立帳戶」跑完再加總，看不到真實帳戶在
# 「同時持倉上限＋共享資金＋組合風險上限」下的樣子。這支改成「單一共享帳戶、跨檔逐日
# 模擬」：每個交易日先處理出場，再從所有尚未持有的個股找當日候選訊號、依分數排序，在
# 「同時持倉數≤上限、進場成本≤剩餘資金、全部最壞同時停損≤組合硬上限」三個限制下開倉，
# 逐日 mark-to-market 出一條真實的帳戶權益曲線。只用當日(含)以前的資料，無未來函數。
def portfolio_backtest_tw(tickers=None, min_score=None, max_positions=None,
                          max_portfolio_risk=None, progress_cb=None) -> Dict:
    from data_fetcher  import fetch_ohlcv
    from signal_engine import check_multi_timeframe_tw, calc_stop_loss_tw, calc_take_profits_tw, calc_position_size
    from stock_universe import get_tw50_components, get_stock_info
    from scoring_engine import calc_performance_metrics
    from config import ACCOUNT_BALANCE_TWD, MAX_SIMULTANEOUS_POSITIONS, MAX_PORTFOLIO_RISK, CIRCUIT_BREAKER
    LOOKBACK = 130
    EXPIRE_DAYS = CIRCUIT_BREAKER.get("signal_expire_days", 15)   # ★ 2026-11-09 Phase B：回測也套用到期窗
    min_score = min_score if min_score is not None else THRESH["min_score"]
    max_pos   = int(max_positions or MAX_SIMULTANEOUS_POSITIONS)
    port_risk = float(max_portfolio_risk if max_portfolio_risk is not None else MAX_PORTFOLIO_RISK)
    targets = tickers or get_tw50_components()
    # 1) 載入各檔日線＋基本資料；建日期→索引對照
    data = {}
    for idx, t in enumerate(targets):
        try:
            d = fetch_ohlcv(t, "daily")
            if d and len(d.get("closes", [])) > LOOKBACK + 20:
                info = get_stock_info(t) or {}
                data[t] = {"d": d, "size_cat": info.get("size_cat", "中型股"),
                           "vol_lots": info.get("volume_lots"),
                           "idx": {dt: i for i, dt in enumerate(d["dates"])}}
            if progress_cb:
                try: progress_cb(idx + 1, len(targets), t)
                except Exception: pass
        except Exception as e:
            logger.warning(f"[PortBT] {t} 載入失敗：{e}")
    if not data:
        return {"error": "資料不足以進行組合級回測"}
    all_dates = sorted({dt for v in data.values() for dt in v["d"]["dates"]})

    balance = float(ACCOUNT_BALANCE_TWD)
    invested = 0.0
    open_pos = {}
    trades, equity, axis, concur = [], [], [], []
    import execution
    n_unfilled_limit = 0

    def _hold(day, p):
        try:
            return (datetime.strptime(day[:10], "%Y-%m-%d") - datetime.strptime(p["bar_date"][:10], "%Y-%m-%d")).days
        except Exception:
            return 0

    def _book(t, legs, day, res_override=None):
        """把這根新成交的腿入帳；部位出清時記一筆完整交易。"""
        nonlocal balance, invested
        p = open_pos[t]
        for leg in legs:
            balance += leg["pnl"]; invested -= leg["shares"] * p["fill_price"]
            p["realized"] += leg["pnl"]
        if p["pos"].remaining > 0:
            return
        open_pos.pop(t)
        sh = sum(l["shares"] for l in p["pos"].legs) or 1
        trades.append({"ticker": t, "direction": p["direction"], "entry": p["fill_price"],
                       "close": round(sum(l["price"] * l["shares"] for l in p["pos"].legs) / sh, 2),
                       "result": res_override or p["pos"].result(), "pnl_twd": round(p["realized"], 0),
                       "shares": p["shares"], "score": p["score"], "bar_in": p["bar_date"], "bar_out": day,
                       "hold_days": _hold(day, p),
                       "legs": [{"stage": l["stage"], "price": l["price"], "shares": l["shares"],
                                 "pnl": round(l["pnl"], 0)} for l in p["pos"].legs]})

    def _bar(dd, di):
        return {"bar_date": dd["dates"][di], "open": dd["opens"][di], "high": dd["highs"][di],
                "low": dd["lows"][di], "close": dd["closes"][di]}

    # ★ 2026-11-12（Q-P0-01／02／03）與正式流程對齊：
    #   時序：盤後掃描用「前一交易日（含）以前」的日 K 產生訊號，計畫價位（進場參考／停損／三段
    #         停利）以訊號日收盤定錨；隔日『開盤』成交。原本用隔日『收盤』進場並以該收盤重算
    #         停損停利，比正式流程晚了一個交易時段、價位基準也不同。
    #   出場：改用 execution.StagedPosition（TP1/TP2/TP3 各 1/3、移動停損、到期窗），與正式推播、
    #         影子 Champion 軌同一個狀態機；原本只在 TP1 一次全出。進場當日即納入出場判定。
    #   成本：每一腿以實際持有天數計借券費（原本 calc_tw_pnl 未傳 days_held，放空借券費恆為 0）。
    for day in all_dates:
        # 2a) 進場（開盤）：以前一交易日（含）以前的資料產生訊號，在三個限制下開倉
        if len(open_pos) < max_pos:
            cands = []
            for t, v in data.items():
                if t in open_pos:
                    continue
                di = v["idx"].get(day)
                if di is None or di < LOOKBACK:
                    continue
                dd = v["d"]
                window = {k: dd[k][:di] for k in ("closes", "highs", "lows", "opens", "volumes")}
                mtf = check_multi_timeframe_tw({"daily": window})
                dir_ = mtf.get("direction", "none"); sc = mtf.get("score", 0)
                if dir_ == "none" or sc < min_score or mtf.get("adx_value", 0) < THRESH["min_adx"]:
                    continue
                price = dd["closes"][di - 1]                     # 訊號日收盤＝計畫價位的定錨
                fill = dd["opens"][di]                           # 隔日開盤成交
                dind = mtf.get("entry_indicators", {}); atr = dind.get("atr", {}).get("value", 0) or price * 0.02
                if not atr or price <= 0 or not fill or fill <= 0:
                    continue
                lo5 = min(dd["lows"][max(0, di - 5):di]) if di >= 5 else None
                hi5 = max(dd["highs"][max(0, di - 5):di]) if di >= 5 else None
                sl = calc_stop_loss_tw(dir_, price, atr, dind, v["size_cat"], lo5, hi5)
                if abs(price - sl) / price * 100 > 12:
                    continue
                tp = calc_take_profits_tw(dir_, price, sl, v["size_cat"])
                if tp["rr1"] < THRESH["min_rr"]:
                    continue
                cands.append((sc, t, dir_, price, fill, sl, tp))
            cands.sort(key=lambda z: -z[0])
            risk_open = sum(p["risk"] * p["pos"].remaining / p["shares"] for p in open_pos.values())
            for sc, t, dir_, price, fill, sl, tp in cands:
                if len(open_pos) >= max_pos:
                    break
                pos = calc_position_size(price, sl, balance=balance, size_cat=data[t]["size_cat"],
                                         avg_volume_lots=data[t]["vol_lots"])
                sh = pos["shares"]
                if sh <= 0:
                    continue
                value = sh * fill; risk = sh * abs(fill - sl)
                if risk <= 0:                              # 開盤已穿越停損：不進場（無有效風險定義）
                    continue
                if execution.entry_blocked_by_limit(dir_, _bar(data[t]["d"], data[t]["idx"][day]), price):
                    n_unfilled_limit += 1                  # 進場當根漲跌停鎖死：確定無法成交，不進場
                    continue
                if invested + value > balance:            # 買力不足
                    continue
                if risk_open + risk > balance * port_risk:  # 組合風險硬上限
                    continue
                di = data[t]["idx"][day]
                sig_day = data[t]["d"]["dates"][di - 1]
                open_pos[t] = {"direction": dir_, "fill_price": fill, "shares": sh, "risk": risk,
                               "bar_date": sig_day, "score": sc, "realized": 0.0,
                               "pos": execution.StagedPosition(fill, sl, tp["tp1"], tp.get("tp2"), tp.get("tp3"),
                                                               dir_, sh, sig_day, calc_tw_pnl, EXPIRE_DAYS,
                                                               prev_close=price)}
                invested += value; risk_open += risk
        # 2b) 出場：所有持倉（含今日新進場者）以當日 K 棒逐腿判定
        for t in list(open_pos.keys()):
            di = data[t]["idx"].get(day)
            if di is None:
                continue
            legs = open_pos[t]["pos"].on_bar(_bar(data[t]["d"], di))
            if legs:
                _book(t, legs, day)
        # 2c) 逐日 mark-to-market（僅未出場的剩餘股數；已出場各腿損益已入 balance）
        unreal = 0.0
        for t, p in open_pos.items():
            di = data[t]["idx"].get(day)
            cl = data[t]["d"]["closes"][di] if di is not None else p["fill_price"]
            unreal += calc_tw_pnl(p["fill_price"], cl, p["direction"], p["pos"].remaining,
                                  days_held=_hold(day, p))
        equity.append(round(balance + unreal, 0)); axis.append(day); concur.append(len(open_pos))
    # 3) 期末強制平倉剩餘部位（以各檔最後一根收盤）
    for t in list(open_pos.keys()):
        dd = data[t]["d"]
        leg = open_pos[t]["pos"].force_close(_bar(dd, len(dd["dates"]) - 1), stage="eow")
        _book(t, [leg] if leg else [], dd["dates"][-1],
              res_override=None if any(l["stage"] != "eow" for l in open_pos[t]["pos"].legs) else "eow")
    # 4) 指標
    metrics = calc_performance_metrics(equity, trades)
    wins = len([x for x in trades if x["pnl_twd"] > 0]); losses = len([x for x in trades if x["pnl_twd"] <= 0])
    def _dir(dd):
        ts = [x for x in trades if x["direction"] == dd]
        w = len([x for x in ts if x["pnl_twd"] > 0])
        return {"n_trades": len(ts), "n_wins": w, "n_losses": len(ts) - w,
                "win_rate": round(w / max(len(ts), 1) * 100, 1),
                "total_pnl_twd": round(sum(x["pnl_twd"] for x in ts), 0)}
    return {"method": "portfolio", "n_tickers": len(data), "n_days": len(axis),
            "initial_balance": round(ACCOUNT_BALANCE_TWD, 0), "final_balance": round(balance, 0),
            "return_pct": round((balance - ACCOUNT_BALANCE_TWD) / ACCOUNT_BALANCE_TWD * 100, 2),
            "n_trades": len(trades), "n_wins": wins, "n_losses": losses,
            "win_rate": round(wins / max(wins + losses, 1) * 100, 1),
            "total_pnl_twd": round(sum(x["pnl_twd"] for x in trades), 0),
            "sharpe": metrics.get("sharpe", 0), "max_drawdown": metrics.get("max_drawdown", 0),
            "calmar": metrics.get("calmar", 0), "annual_return": metrics.get("annual_return", 0),
            "max_concurrent": max(concur) if concur else 0,
            "avg_concurrent": round(sum(concur) / max(len(concur), 1), 2),
            "max_positions": max_pos, "max_portfolio_risk_pct": round(port_risk * 100, 1),
            "min_score_used": min_score, "by_direction": {"buy": _dir("buy"), "sell": _dir("sell")},
            "equity_curve": equity, "dates": axis, "trades": trades[-50:],
            "exit_model": "staged", "sim_version": execution.STAGED_SIM_VERSION,
            "entry_model": "訊號日收盤定錨計畫價位、隔日開盤成交",
            "universe_pit": "PIT_UNAVAILABLE", "n_unfilled_limit": n_unfilled_limit,
            "limitations": [
                "標的池與規模分類取自『現在』的成分股與基本資料，非歷史當時（倖存者／時點偏差），"
                "績效可能高估；不得當成正式有效的歷史績效。",
                "已模擬：一價鎖死的漲跌停（進場不成交、出場順延）與停牌（當日無 K 棒即不交易）。"
                "未模擬：盤中曾打開的漲跌停排隊、成交量上限、放空券源與整股限制。",
                "放空借券費以固定年率估算，未反映個股實際費率與券源有無。"],
            "note": "單一共享帳戶、跨檔逐日模擬，受同時持倉上限／買力／組合風險硬上限約束；"
                    "出場為 TP1/TP2/TP3 各 1/3 的分批出場（與正式推播同一狀態機）；"
                    "成本含手續費、證交稅、滑價、放空借券費（calc_tw_pnl）。訊號只用前一交易日(含)以前資料。",
            "completed_at": datetime.now(timezone.utc).isoformat()}


# ★ 新增：2026-09-24——使用者要求「用新增的東西做詳細回測，跟過去策略比較
# 勝率/準確度有沒有提升」，並在看到第一版（只有TWII一項）差異很小之後，
# 明確要求「用真實資料回測，不要假數據，能補的都補上」。這裡把「有沒有套用
# 總經加權 + 基本面硬性過濾」的兩次全市場回測包成一個函式，跑兩輪
# run_full_backtest_tw()（一輪全部 False 當作「過去策略」基準，一輪全部 True
# 當作「新策略」），回傳兩邊的整體統計＋差異。
def run_comparison_backtest_tw(tickers=None, min_score=65.0, progress_cb=None) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components()
    def _cb_baseline(done,total,ticker):
        if progress_cb: progress_cb(done,total*2,f"[基準/舊策略] {ticker}")
    def _cb_overlay(done,total,ticker):
        if progress_cb: progress_cb(total+done,total*2,f"[新策略/總經+基本面] {ticker}")
    baseline=run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb_baseline,
                                   use_macro_overlay=False,use_fundamentals_filter=False)
    overlay =run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb_overlay,
                                   use_macro_overlay=True, use_fundamentals_filter=True)
    def _delta(a,b): return round(b-a,2)
    comparison={
        "win_rate_before":baseline.get("overall_win_rate",0),
        "win_rate_after":overlay.get("overall_win_rate",0),
        "win_rate_delta":_delta(baseline.get("overall_win_rate",0),overlay.get("overall_win_rate",0)),
        "n_trades_before":baseline.get("n_trades_total",0),
        "n_trades_after":overlay.get("n_trades_total",0),
        "avg_sharpe_before":baseline.get("avg_sharpe",0),
        "avg_sharpe_after":overlay.get("avg_sharpe",0),
        "avg_sharpe_delta":_delta(baseline.get("avg_sharpe",0),overlay.get("avg_sharpe",0)),
        "avg_return_pct_before":baseline.get("avg_return_pct",0),
        "avg_return_pct_after":overlay.get("avg_return_pct",0),
        "avg_return_pct_delta":_delta(baseline.get("avg_return_pct",0),overlay.get("avg_return_pct",0)),
        "by_direction_before":baseline.get("by_direction_aggregate",{}),
        "by_direction_after":overlay.get("by_direction_aggregate",{}),
        # ★ 修正：2026-09-26（稽核 finding #6）——原本這裡寫「個股層級(T86)雖支援
        # 但換算全市場金額需要逐股乘價格加總、成本與風險不成比例，此項不做」，
        # 這個理由其實是講錯了目標：signal_engine.check_multi_timeframe_tw()
        # 裡實際會用到的個股法人加減分（±5，見該檔案298-307行）根本不需要換算
        # 成「金額」，直接比較 fetch_stock_institutional() 回傳的個股外資買賣超
        # 「張數」是否 >200／<-200 即可，跟這裡原本講的「逐股乘價格加總」是
        # 兩件不同的事——這條理由沒有正確反映「為什麼沒做」。真正的原因是：
        # data_fetcher.fetch_institutional_flow(date_str) 雖然技術上支援查
        # 任意歷史日期，但這是「每次呼叫對 TWSE 發一次即時請求」的介面，而
        # _fetch_margin_chg_map() 的既有設計原則明確是「回測不該在迴圈裡對
        # TWSE 發出幾百次即時請求，那是回填腳本的工作，回測這裡只負責查表」
        # （見上方 _fetch_margin_chg_map() 說明）——法人買賣超目前沒有像融資
        # 餘額(MI_MARGN)或月營收(MOPS)那樣的歷史回填腳本/資料表，要合規地把
        # 個股法人加減分納入回測，需要先建一支法人資料的歷史回填腳本（比照
        # backfill_margin_history()），而不是在這裡的回測迴圈裡直接即時查
        # T86。這是尚未做的基礎建設，不是技術上做不到，先誠實記錄，之後若要
        # 補上，入口點在這裡跟 backtest_symbol_tw()/backtest_symbol_tw_partial()
        # 的進場邏輯（等 macro_adj 一樣加一項 inst_adj）。
        "scope_note":("此比較涵蓋：大盤(TWII)熔斷（yfinance真實歷史指數）、融資餘額日增減"
                       "（TWSE MI_MARGN真實歷史回填）、個股月營收硬性過濾（MOPS真實歷史回填，"
                       "point-in-time查詢避免看未來資料）。仍未涵蓋：個股法人買賣超±5分加減分"
                       "（見 signal_engine.check_multi_timeframe_tw）——目前沒有像融資餘額/月營收"
                       "那樣的歷史回填資料表，只能即時查詢單日 TWSE T86，違反「回測迴圈不對外部"
                       "API發出幾百次即時請求」的既有設計原則（見 backtester._fetch_margin_chg_map()"
                       "說明），需要先建立法人資料歷史回填腳本才能合規補上，目前尚未實作。K線"
                       "持久化快取是純基礎設施，不影響訊號邏輯，不需要回測。若融資/月營收回填"
                       "尚未執行，這兩項在比較中會跟沒有異常一樣（不生效，不是假裝觸發），"
                       "不會虛增差異。"),
    }
    return {"completed_at":datetime.now(timezone.utc).isoformat(),"min_score":min_score,
            "n_tickers":len(targets),"baseline":baseline,"overlay":overlay,"comparison":comparison}


# ★ 新增：2026-09-26——稽核報告中長期項目「因子/評分消融分析」。過去只知道
# check_multi_timeframe_tw() 有 EMA排列/半年線突破/RSI/MACD/量增/ADX加成/
# 週線逆勢懲罰 共7項會影響分數，但從來沒有量化過「拿掉某一項，勝率/報酬
# 到底會不會變差、變好多少」——換句話說，不知道這7項裡哪些是真的有貢獻的
# 訊號、哪些只是加了分數卻沒有實際預測力（甚至可能是雜訊）。做法：先用
# 「全部因子都開啟」（=目前實盤真正在用的邏輯，等同 baseline）跑一次全市場
# 回測，再逐一把每個因子丟進 signal_engine.check_multi_timeframe_tw() 的
# disabled_factors 關掉、各自重跑一次回測，比較「拿掉這項之後」勝率/總報酬/
# Sharpe 跟 baseline 的差異——差異越大（拿掉後表現變差越多），代表這項因子
# 的邊際貢獻越高；如果拿掉某項之後表現反而變好或幾乎不變，代表這項因子目前
# 的加分規則可能沒有實際預測力，值得之後重新檢視或調整權重。
# 注意：因為是「開/關同一個因子共8輪全市場回測」，執行時間約為單次
# run_full_backtest_tw 的 8 倍，一定會超過 gunicorn 逾時，app.py 比照
# full_backtest/compare_backtest 用背景執行緒＋輪詢進度處理。
def run_factor_ablation_tw(tickers=None, min_score=65.0, progress_cb=None) -> Dict:
    from stock_universe import get_tw50_components
    from signal_engine import FACTOR_KEYS
    targets=tickers or get_tw50_components()
    n_stages=len(FACTOR_KEYS)+1
    def _cb(stage_idx, stage_label):
        def _inner(done,total,ticker):
            if progress_cb: progress_cb(stage_idx,n_stages,f"[{stage_label}] {ticker} ({done}/{total})")
        return _inner
    logger.info(f"[BT-ABLATION] 開始因子消融分析，共 {len(targets)} 檔 × {n_stages} 輪（baseline + {len(FACTOR_KEYS)} 個因子逐一關閉）")
    baseline=run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb(0,"baseline 全因子開啟"))
    factor_results={}
    for i,factor in enumerate(FACTOR_KEYS):
        r=run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb(i+1,f"關閉「{factor}」"),
                                disabled_factors={factor})
        factor_results[factor]=r
    def _delta(a,b): return round(b-a,2)
    factor_zh={"ema":"EMA排列","trend200":"半年線突破","rsi":"RSI區間","macd":"MACD動能",
               "volume":"成交量確認","adx":"ADX趨勢強度加成","weekly":"週線逆勢懲罰"}
    ablation_table=[]
    base_wr=baseline.get("overall_win_rate",0); base_sharpe=baseline.get("avg_sharpe",0)
    base_ret=baseline.get("avg_return_pct",0); base_n=baseline.get("n_trades_total",0)
    for factor in FACTOR_KEYS:
        r=factor_results[factor]
        wr=r.get("overall_win_rate",0); sharpe=r.get("avg_sharpe",0); ret=r.get("avg_return_pct",0)
        ablation_table.append({
            "factor":factor,"factor_zh":factor_zh.get(factor,factor),
            "win_rate_without":wr,"win_rate_delta_if_removed":_delta(base_wr,wr),
            "avg_sharpe_without":sharpe,"avg_sharpe_delta_if_removed":_delta(base_sharpe,sharpe),
            "avg_return_pct_without":ret,"avg_return_pct_delta_if_removed":_delta(base_ret,ret),
            "n_trades_without":r.get("n_trades_total",0),
            "interpretation":("移除後表現變差，此因子有正貢獻" if wr<base_wr
                               else "移除後表現變好或持平，此因子目前的加分規則可能沒有實際預測力，建議重新檢視"),
        })
    ablation_table.sort(key=lambda x:x["win_rate_delta_if_removed"])
    return {"completed_at":datetime.now(timezone.utc).isoformat(),"min_score":min_score,"n_tickers":len(targets),
            "baseline":{"win_rate":base_wr,"avg_sharpe":base_sharpe,"avg_return_pct":base_ret,"n_trades_total":base_n},
            "ablation_table":ablation_table,
            "note":("每一列代表「把這個因子關掉之後」重跑全市場回測的結果，win_rate_delta_if_removed 等"
                    "欄位＝baseline減去關閉後的數值，正值代表關掉後表現變差（該因子有貢獻），負值或接近0"
                    "代表關掉後表現持平甚至變好（該因子目前可能沒有實際預測力）。此分析只消融評分因子本身，"
                    "不涉及總經加權/基本面過濾（那兩項見 run_comparison_backtest_tw）。")}


# ★ 新增：2026-09-26——稽核報告中長期項目「真正的分批出場回測邏輯」。系統
# 每次推播訊號都明確跟使用者說 exit_plan="各1/3分批出場"（見 calc_take_profits_tw），
# 但過去所有回測（backtest_symbol_tw/walk_forward_backtest_tw）跟實際的
# /fill 損益追蹤，都是「單一出場價」模型：不管訊號寫的是分批出場，回測跟
# 統計上都是「碰到 SL 就全部出場算輸，碰到 TP1 或 TP2（先碰到哪個就算哪個）
# 就全部出場算贏」，2026-09-16 的稽核已經誠實揭露過這個落差（見 telegram_bot.py
# 的揭露文字），但從未真正實作過分批出場的回測，所以顯示給使用者的勝率/
# 總報酬其實都不是「如果真的照建議分批出場」會得到的數字。
# 這裡新增一套真正逐段模擬的回測：
#   - 部位依約 1/3、1/3、1/3（股數無法整除3時，餘數併入第三段）分成三段。
#   - 價格碰到 TP1：出清第一段，剩餘部位停損移到「保本價」(=進場價)，之後
#     即使反轉也不會讓已經到手的第一段獲利被侵蝕成整體虧損。
#   - 價格碰到 TP2：出清第二段，剩餘（第三段）停損進一步移到 TP1（鎖住
#     第一、二段+部分第三段的獲利）。
#   - 價格碰到 TP3：出清最後一段，部位完全平倉。
#   - 若當下停損價（原始SL，或移動後的保本價/TP1）被觸發，剩餘尚未出清的
#     股數全部依當下停損價出場。
#   - 同一天K棒的高低點若橫跨多段目標（日K圖無法得知盤中真實觸價順序），
#     依序由近到遠檢查（跟本檔案其他函式對「同一根K棒內SL/TP先後」的既有
#     簡化假設一致，不是這裡才新發明的方法論）。
# 進場邏輯（訊號產生/停損停利計算/部位大小）完全複用跟實盤同一套
# check_multi_timeframe_tw／calc_stop_loss_tw／calc_take_profits_tw／
# calc_position_size，只有出場模擬邏輯不同，避免出現「回測用另一套進場
# 判斷」的老問題。
def backtest_symbol_tw_partial(ticker, initial_balance=None, min_score=None,
                                use_macro_overlay=False, use_fundamentals_filter=False) -> Dict:
    from data_fetcher   import fetch_ohlcv
    from signal_engine  import check_multi_timeframe_tw, calc_stop_loss_tw, calc_take_profits_tw, calc_position_size
    from scoring_engine import calc_performance_metrics
    min_score = min_score if min_score is not None else THRESH["min_score"]
    balance=initial_balance or ACCOUNT_BALANCE_TWD
    data=fetch_ohlcv(ticker,"daily")
    if not data or len(data.get("closes",[]))<60:
        return {"error":f"{ticker} 歷史數據不足（需60根日線）"}
    closes=data["closes"]; highs=data["highs"]; lows=data["lows"]; opens=data["opens"]; volumes=data["volumes"]
    dates=data.get("dates",[])
    weekly_raw=fetch_ohlcv(ticker,"weekly")
    w_dates  =weekly_raw.get("dates",[])   if weekly_raw else []
    w_closes =weekly_raw.get("closes",[])  if weekly_raw else []
    w_highs  =weekly_raw.get("highs",[])   if weekly_raw else []
    w_lows   =weekly_raw.get("lows",[])    if weekly_raw else []
    w_opens  =weekly_raw.get("opens",[])   if weekly_raw else []
    w_volumes=weekly_raw.get("volumes",[]) if weekly_raw else []
    def _weekly_asof(day_date):
        # ★ 修正：2026-09-29——跟backtest_symbol_tw()裡同名函式同一個修正：
        # 排除day_date當天所屬、可能還沒走完的那一根週線K棒，避免look-ahead
        # bias（完整理由見backtest_symbol_tw()裡_weekly_asof()的docstring）。
        if not w_dates or not day_date: return None
        w_idx=bisect.bisect_right(w_dates, day_date)-1
        if w_idx<20: return None
        return {"closes":w_closes[:w_idx],"highs":w_highs[:w_idx],"lows":w_lows[:w_idx],
                "opens":w_opens[:w_idx],"volumes":w_volumes[:w_idx]}
    try:
        from stock_universe import get_stock_info
        info=get_stock_info(ticker); size_cat=info.get("size_cat","中型股") if info else "中型股"
    except Exception:
        size_cat="中型股"
    n=len(closes); LOOKBACK=130; trades=[]; equity=[balance]; open_trade=None
    twii_chg_map=_fetch_twii_change_map() if use_macro_overlay else {}
    margin_chg_map=_fetch_margin_chg_map() if use_macro_overlay else {}
    fund_code=ticker.split(".")[0] if use_fundamentals_filter else None
    if use_fundamentals_filter:
        from fundamentals import check_fundamental_hard_filter_asof
    logger.info(f"[BT-PARTIAL] {ticker} 開始分批出場回測，共 {n} 根日線，size_cat={size_cat}")
    import execution
    EXPIRE_DAYS=_bt_expire_days()
    def _bar(i):
        return {"bar_date":dates[i] if i<len(dates) else "","open":opens[i] if i<len(opens) else None,
                "high":highs[i],"low":lows[i],"close":closes[i]}
    def _record(i):
        nonlocal open_trade
        d=open_trade["direction"]
        legs=[{"stage":l["stage"],"price":l["price"],"shares":l["shares"],"pnl_twd":l["pnl"]} for l in open_trade["pos"].legs]
        total_pnl=sum(l["pnl_twd"] for l in legs)
        trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],
                        "result":"+".join(l["stage"] for l in legs) or "flat","pnl_twd":total_pnl,
                        "shares":open_trade["shares_total"],
                        "pnl_pct":round(total_pnl/max(balance-total_pnl,1)*100,2),
                        "score":open_trade.get("score",0),"legs":legs,
                        "n_legs_hit":sum(1 for l in legs if l["stage"] in ("tp1","tp2","tp3")),
                        "bar_in":open_trade["bar"],"bar_out":i,"hold_days":i-open_trade["bar"]})
        open_trade=None
    def _step(i):
        """餵一根 K 棒給分批狀態機；各腿損益即時入帳，出清即記一筆交易。回傳是否已出清。"""
        nonlocal balance
        for leg in open_trade["pos"].on_bar(_bar(i)):
            balance+=leg["pnl"]
        if open_trade["pos"].remaining<=0:
            _record(i); return True
        return False
    for i in range(LOOKBACK,n):
        wd={"closes":closes[:i],"highs":highs[:i],"lows":lows[:i],"opens":opens[:i],"volumes":volumes[:i]}
        day_date=dates[i] if i<len(dates) else None
        price=closes[i]
        plan_px,fill_px=_bt_entry_prices(closes,opens,i)
        just_exited=_step(i) if open_trade else False
        if open_trade is None and not just_exited and plan_px is not None:
            tf_data_bt={"daily":wd}
            w_slice=_weekly_asof(day_date)
            if w_slice: tf_data_bt["weekly"]=w_slice
            mtf=check_multi_timeframe_tw(tf_data_bt)
            direction=mtf.get("direction","none")
            fund_blocked=False
            if direction!="none" and use_fundamentals_filter and day_date:
                fund_chk=check_fundamental_hard_filter_asof(fund_code, day_date)
                fund_blocked=fund_chk.get("blocked", False)
            if direction!="none" and not fund_blocked:
                score=mtf.get("score",0)
                if use_macro_overlay:
                    macro_adj=_macro_score_adj(day_date, twii_chg_map, margin_chg_map)
                    if macro_adj: score=max(0,score+macro_adj)
                if score>=min_score:
                    adx_val=mtf.get("adx_value",0)
                    if adx_val>=THRESH["min_adx"]:
                        vol_ratio=mtf.get("vol_ratio",1.0)
                        if not(vol_ratio<THRESH["min_vol_ratio"] and score<75):
                            daily_ind=mtf.get("entry_indicators",{})
                            atr=daily_ind.get("atr",{}).get("value",0) or plan_px*0.02
                            if atr:
                                low_5d=min(lows[max(0,i-5):i]) if i>=5 else None
                                high_5d=max(highs[max(0,i-5):i]) if i>=5 else None
                                sl=calc_stop_loss_tw(direction,plan_px,atr,daily_ind,size_cat,low_5d,high_5d)
                                tp_info=calc_take_profits_tw(direction,plan_px,sl,size_cat)
                                # ★ 修正：2026-09-26——同 backtest_symbol_tw() 補上的兩項，跟實盤
                                # generate_signal_tw() 對齊：停損距離>12%（追高過濾）就跳過；
                                # calc_position_size() 補上 avg_volume_lots 流動性上限。
                                sl_dist_pct=abs(plan_px-sl)/plan_px*100 if plan_px else 0
                                if tp_info["rr1"]>=THRESH["min_rr"] and sl_dist_pct<=12:
                                    pos=calc_position_size(plan_px,sl,balance=balance,size_cat=size_cat,
                                                            avg_volume_lots=(info.get("volume_lots") if info else None))
                                    shares_total=pos["shares"]
                                    if shares_total>0 and _bt_fill_valid(direction,fill_px,sl,tp_info["tp1"],_bar(i),plan_px):
                                        # 與正式推播、組合回測同一個分批狀態機（TP1/TP2/TP3 各 1/3、移動停損、
                                        # 跳空以開盤成交、到期窗）；部位太小無法分三段時自然退化為 TP3 單段。
                                        open_trade={"direction":direction,"fill_price":fill_px,"score":score,
                                                    "shares_total":shares_total,"bar":i,"sig_bar":i-1,
                                                    "pos":execution.StagedPosition(
                                                        fill_px,sl,tp_info["tp1"],tp_info["tp2"],tp_info["tp3"],
                                                        direction,shares_total,
                                                        dates[i-1] if i-1<len(dates) else "",calc_tw_pnl,EXPIRE_DAYS,
                                                        prev_close=plan_px)}
                                        _step(i)              # 進場當日即納入出場判定
        if open_trade:
            unreal=calc_tw_pnl(open_trade["fill_price"],price,open_trade["direction"],open_trade["pos"].remaining,days_held=_held_days(dates,open_trade["sig_bar"],i))
            equity.append(balance+unreal)
        else:
            equity.append(balance)
    if open_trade:
        leg=open_trade["pos"].force_close(_bar(n-1),stage="forced_close")
        if leg: balance+=leg["pnl"]
        _record(n-1)
    metrics=calc_performance_metrics(equity,trades)
    wins=[t for t in trades if t["pnl_twd"]>0]
    losses=[t for t in trades if t["pnl_twd"]<=0]
    init_bal=initial_balance or ACCOUNT_BALANCE_TWD
    full3_exits=[t for t in trades if t.get("n_legs_hit",0)==3]
    return {"ticker":ticker,"n_bars":n,"n_trades":len(trades),"n_wins":len(wins),"n_losses":len(losses),
            "win_rate":round(len(wins)/max(len(trades),1)*100,1),
            "total_pnl_twd":round(sum(t["pnl_twd"] for t in trades),0),
            "initial_balance":init_bal,"final_balance":round(balance,0),
            "return_pct":round((balance-init_bal)/init_bal*100,2),
            "avg_hold_days":round(sum(t.get("hold_days",0) for t in trades)/max(len(trades),1),1),
            "sharpe":metrics.get("sharpe",0),"max_drawdown":metrics.get("max_drawdown",0),
            "calmar":metrics.get("calmar",0),"annual_return":metrics.get("annual_return",0),
            "max_consec_loss":metrics.get("max_consec_loss",0),
            "n_trades_full3_exit":len(full3_exits),
            "pct_trades_full3_exit":round(len(full3_exits)/max(len(trades),1)*100,1),
            "equity_curve":equity,"trades":trades[-30:],"min_score_used":min_score,
            "grade":metrics.get("sharpe_grade","—"),"dd_grade":metrics.get("dd_grade","—"),
            "method":"partial_exit_thirds_breakeven_trail",
            "entry_model":"訊號日收盤定錨計畫價位、隔日開盤成交","expire_days":EXPIRE_DAYS,
            "exit_model":"staged","sim_version":execution.STAGED_SIM_VERSION,"universe_pit":"PIT_UNAVAILABLE",
            "completed_at":datetime.now(timezone.utc).isoformat()}

def run_full_backtest_tw_partial(tickers=None, min_score=65.0, progress_cb=None) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components(); results=[]
    for idx,ticker in enumerate(targets):
        try:
            r=backtest_symbol_tw_partial(ticker,min_score=min_score)
            if "error" not in r: results.append(r)
            if progress_cb:
                try: progress_cb(idx+1,len(targets),ticker)
                except Exception: pass
            time.sleep(1.0)
        except Exception as e: logger.error(f"[BT-PARTIAL] {ticker} 失敗: {e}")
    results.sort(key=lambda x:x.get("sharpe",0),reverse=True)
    n_trades_total=sum(r.get("n_trades",0) for r in results)
    n_wins_total=sum(r.get("n_wins",0) for r in results)
    overall_win_rate=round(n_wins_total/max(n_trades_total,1)*100,1)
    avg_sharpe=round(sum(r.get("sharpe",0) for r in results)/max(len(results),1),2)
    avg_return_pct=round(sum(r.get("return_pct",0) for r in results)/max(len(results),1),2)
    pct_full3=round(sum(r.get("n_trades_full3_exit",0) for r in results)/max(n_trades_total,1)*100,1)
    return {"total":len(results),"completed_at":datetime.now(timezone.utc).isoformat(),
            "method":"partial_exit_thirds_breakeven_trail",
            "overall_win_rate":overall_win_rate,"n_trades_total":n_trades_total,
            "avg_sharpe":avg_sharpe,"avg_return_pct":avg_return_pct,
            "pct_trades_full3_exit":pct_full3,
            "note":("每筆交易依訊號公告的「各1/3分批出場」計畫實際模擬：第一段碰TP1出清＋停損移保本，"
                    "第二段碰TP2出清＋停損移至TP1，第三段碰TP3出清或被移動後的停損打到。win_rate 定義"
                    "為「整筆交易（三段加總）淨損益是否為正」，不是「有沒有碰到任一個TP」。可與"
                    "run_full_backtest_tw()（單一出場價模型）的結果對照，量化過去「各1/3分批出場」"
                    "文案跟實際回報數字之間的落差。"),
            "leaderboard":[{"ticker":r["ticker"],"win_rate":r["win_rate"],"sharpe":r["sharpe"],
                            "max_drawdown":r["max_drawdown"],"total_pnl_twd":r["total_pnl_twd"],
                            "return_pct":r["return_pct"],"grade":r["grade"],"n_trades":r["n_trades"],
                            "pct_trades_full3_exit":r.get("pct_trades_full3_exit",0)} for r in results],
            "details":results}


# ★ 新增：2026-09-28——item 3（walk-forward/OOS 驗證放空分層閘門，ChatGPT/
# Perplexity 第二輪都建議「先驗證新閘門有沒有效，再動 alpha_score」）。這裡
# 用「有沒有套用 Layer1 Market Regime Gate + Layer2 ADX方向確認」跑兩輪全市場
# 回測（use_short_gates=False 當「舊邏輯」基準，True 當「新閘門」），只聚焦
# 放空方向的統計差異——因為這兩層閘門只影響放空訊號的產生，做多完全不受
# 影響（by_direction["buy"] 兩輪理論上應該一致，這裡也一併回傳方便交叉核對）。
# 這是「樣本外」的意義：新閘門本身是2026-09-28才上線的邏輯，過去實際交易
# 從未真的套用過，這裡用過去(TW50, 近1年)歷史資料重放，是它第一次被拿真實
# 歷史資料檢驗，不是「調參數去擬合看過的資料」那種樣本內過擬合。
def run_short_gate_validation_tw(tickers=None, min_score=65.0, progress_cb=None) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components()
    def _cb_baseline(done,total,ticker):
        if progress_cb: progress_cb(done,total*2,f"[舊邏輯/無分層閘門] {ticker}")
    def _cb_gated(done,total,ticker):
        if progress_cb: progress_cb(total+done,total*2,f"[新閘門/Regime+ADX方向] {ticker}")
    baseline=run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb_baseline,use_short_gates=False)
    gated   =run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb_gated,   use_short_gates=True)
    b_sell=baseline.get("by_direction_aggregate",{}).get("sell",{})
    g_sell=gated.get("by_direction_aggregate",{}).get("sell",{})
    b_buy=baseline.get("by_direction_aggregate",{}).get("buy",{})
    g_buy=gated.get("by_direction_aggregate",{}).get("buy",{})
    def _delta(a,b): return round(b-a,2)
    comparison={
        "sell_n_trades_before":b_sell.get("n_trades",0),
        "sell_n_trades_after":g_sell.get("n_trades",0),
        "sell_win_rate_before":b_sell.get("win_rate",0),
        "sell_win_rate_after":g_sell.get("win_rate",0),
        "sell_win_rate_delta":_delta(b_sell.get("win_rate",0),g_sell.get("win_rate",0)),
        "sell_total_pnl_before":b_sell.get("total_pnl_twd",0),
        "sell_total_pnl_after":g_sell.get("total_pnl_twd",0),
        "sell_avg_pnl_before":b_sell.get("avg_pnl_twd",0),
        "sell_avg_pnl_after":g_sell.get("avg_pnl_twd",0),
        "buy_unaffected_check":{
            "n_trades_before":b_buy.get("n_trades",0),"n_trades_after":g_buy.get("n_trades",0),
            "win_rate_before":b_buy.get("win_rate",0),"win_rate_after":g_buy.get("win_rate",0),
            "note":"★ 修正：2026-09-29——原本這裡寫「理論上應該完全一致，不一致代表bug」是錯的：backtest_symbol_tw() 同一檔股票同一時間只能有一筆 open_trade（單一倉位槽），放空閘門攔掉一筆原本會開倉的sell訊號時，那個時間點的倉位槽是空的，可能被「原本因為槽被那筆sell佔用而進不去」的後續buy訊號填補，所以做多筆數前後不一致是這個單倉位模型的正常結果，不是bug。真正該檢查程式邏輯的情況，是這個差異大到不合理（例如相差好幾倍），或方向明顯說不通。",
        },
        "interpretation":(
            "sell_n_trades大幅下降、sell_win_rate上升＝閘門有效過濾掉了低品質放空訊號（用「少做但做對」換勝率，"
            "符合原本設計動機：解決舊邏輯用單日情緒當唯一開關、ADX不分方向誤放行「強勢多頭」當放空訊號的問題）。"
            "若sell_n_trades掉到個位數甚至0，代表閘門在近1年台灣50這個樣本裡幾乎不曾同時滿足（大盤中期偏空+"
            "週線偏空+ADX方向偏空），這本身也是有意義的結果——不是bug，是「近1年台灣50很少出現這種空頭條件」，"
            "跟目前/api/state看到的大盤multi-month上升趨勢（bullish regime）互相印證。"
        ),
        "scope_note":(
            "只套用 Layer1(大盤regime)/Layer2(週線+ADX方向)兩層硬性閘門，Layer1次要保護"
            "(SHORT_MAX_SENTIMENT單日情緒)因為沒有歷史逐日情緒分數可回填，這裡誠實跳過未套用"
            "（不是假裝一直通過），理由跟 run_comparison_backtest_tw() 對外資買賣超的處理原則一致。"
        ),
    }
    return {"completed_at":datetime.now(timezone.utc).isoformat(),"min_score":min_score,
            "n_tickers":len(targets),"baseline":baseline,"gated":gated,"comparison":comparison}


# ★ 新增：2026-09-29——run_short_gate_validation_tw() 的樣本被 production
# fetch_ohlcv() 綁死在近1年（見上方 _fetch_long_history_tw() 說明），近1年
# 幾乎全程是大盤 bullish regime，「放空筆數 28→0」沒辦法回答「這是閘門真的
# 在空頭有效，還是近1年根本沒出現過閘門會放行的空頭條件」。這裡改用
# _fetch_long_history_tw() 抓長期歷史（預設 period="max"），把回測資料截到
# period_end 為止（避免用區間之後的未來價格強制平倉），只把「新開倉」限制
# 在 [period_start, period_end] 這段指定期間（date_start 之前的資料仍保留、
# 只用來讓 LOOKBACK=130 根日K的暖身期跟週線 _weekly_asof() 有足夠歷史可看，
# 不會被誤用來開倉——見 backtest_symbol_tw() 的 date_start 說明），藉此驗證
# 閘門在真實指定期間（例如2022年台股實際空頭）的表現。
# 為了不對 yfinance 重複發出請求，每檔股票的長期歷史只抓一次（daily+weekly），
# 同一份（已截斷的）資料同時餵給 baseline（use_short_gates=False）跟
# gated（use_short_gates=True）兩輪回測。
def run_short_gate_validation_tw_period(tickers=None, min_score=65.0, period_start="2022-01-01",
                                         period_end="2022-12-31", history_period="max",
                                         progress_cb=None) -> Dict:
    from stock_universe import get_tw50_components
    targets = tickers or get_tw50_components()
    baseline_results = []
    gated_results = []
    skipped = []
    total = len(targets)
    for idx, ticker in enumerate(targets):
        try:
            daily_long = _fetch_long_history_tw(ticker, "daily", history_period)
            weekly_long = _fetch_long_history_tw(ticker, "weekly", history_period)
            daily_trunc = _truncate_ohlcv_to_date(daily_long, period_end)
            weekly_trunc = _truncate_ohlcv_to_date(weekly_long, period_end)
            if not daily_trunc or len(daily_trunc.get("closes", [])) < 60:
                skipped.append({"ticker": ticker, "reason": "長期歷史資料不足（可能是2022年之後才掛牌，或yfinance抓不到）"})
                continue
            # 暖身期檢查：period_start 之前至少要有 LOOKBACK(130) 根日K可用，
            # 否則 period_start 一開始的進場判斷會用到不足130根的殘缺指標，
            # 跟 backtest_symbol_tw() 其他呼叫路徑的資料完整度不一致。
            dates_before_start = [d for d in daily_trunc.get("dates", []) if d < period_start]
            if len(dates_before_start) < 130:
                skipped.append({"ticker": ticker, "reason": f"{period_start} 之前可用日K只有 {len(dates_before_start)} 根（需≥130根暖身），略過此檔避免指標失真"})
                continue
            r_base = backtest_symbol_tw(ticker, min_score=min_score, use_short_gates=False,
                                         daily_override=daily_trunc, weekly_override=weekly_trunc, date_start=period_start)
            r_gate = backtest_symbol_tw(ticker, min_score=min_score, use_short_gates=True,
                                         daily_override=daily_trunc, weekly_override=weekly_trunc, date_start=period_start)
            if "error" not in r_base: baseline_results.append(r_base)
            if "error" not in r_gate: gated_results.append(r_gate)
        except Exception as e:
            logger.error(f"[BT-BEAR] {ticker} 失敗: {e}")
            skipped.append({"ticker": ticker, "reason": f"執行失敗: {e}"})
        if progress_cb:
            try: progress_cb(idx + 1, total, ticker)
            except Exception: pass
        time.sleep(0.3)

    def _agg(results):
        agg = {"buy": {"n_trades": 0, "n_wins": 0, "n_losses": 0, "total_pnl_twd": 0},
               "sell": {"n_trades": 0, "n_wins": 0, "n_losses": 0, "total_pnl_twd": 0}}
        for r in results:
            bd = r.get("by_direction", {})
            for d in ("buy", "sell"):
                s = bd.get(d, {})
                agg[d]["n_trades"] += s.get("n_trades", 0)
                agg[d]["n_wins"] += s.get("n_wins", 0)
                agg[d]["n_losses"] += s.get("n_losses", 0)
                agg[d]["total_pnl_twd"] += s.get("total_pnl_twd", 0)
        for d in ("buy", "sell"):
            wl = agg[d]["n_wins"] + agg[d]["n_losses"]
            agg[d]["win_rate"] = round(agg[d]["n_wins"] / max(wl, 1) * 100, 1)
            agg[d]["avg_pnl_twd"] = round(agg[d]["total_pnl_twd"] / max(agg[d]["n_trades"], 1), 0)
        return agg

    b_agg = _agg(baseline_results)
    g_agg = _agg(gated_results)
    b_sell = b_agg["sell"]; g_sell = g_agg["sell"]
    b_buy = b_agg["buy"]; g_buy = g_agg["buy"]

    def _delta(a, b): return round(b - a, 2)

    comparison = {
        "sell_n_trades_before": b_sell["n_trades"], "sell_n_trades_after": g_sell["n_trades"],
        "sell_win_rate_before": b_sell["win_rate"], "sell_win_rate_after": g_sell["win_rate"],
        "sell_win_rate_delta": _delta(b_sell["win_rate"], g_sell["win_rate"]),
        "sell_total_pnl_before": b_sell["total_pnl_twd"], "sell_total_pnl_after": g_sell["total_pnl_twd"],
        "buy_unaffected_check": {
            "n_trades_before": b_buy["n_trades"], "n_trades_after": g_buy["n_trades"],
            "win_rate_before": b_buy["win_rate"], "win_rate_after": g_buy["win_rate"],
            "note": "★ 修正：2026-09-29——原本這裡寫「理論上應該完全一致，不一致代表bug」是錯的：backtest_symbol_tw() 同一檔股票同一時間只能有一筆 open_trade（單一倉位槽），放空閘門攔掉一筆原本會開倉的sell訊號時，那個時間點的倉位槽是空的，可能被「原本因為槽被那筆sell佔用而進不去」的後續buy訊號填補，所以做多筆數前後不一致是這個單倉位模型的正常結果，不是bug。真正該檢查程式邏輯的情況，是這個差異大到不合理（例如相差好幾倍），或方向明顯說不通。",
        },
        "interpretation": (
            f"這是指定期間（{period_start}～{period_end}）而非近1年的驗證，用意是檢驗放空分層閘門"
            "在真實空頭條件下的表現，而不是只看「近1年很少出現空頭條件」這個可能混淆結論的樣本。"
            "若這段期間 sell_n_trades_before（舊邏輯）明顯>0 而 sell_n_trades_after（新閘門）仍然是0"
            "或極少，且勝率/損益有改善，代表閘門不是「太嚴格導致什麼都沒測到」，而是真的在區分"
            "好/壞放空訊號；若舊邏輯在這段期間本身交易數也很少，代表台灣50這個範圍在這段期間本身"
            "放空訊號觸發就不多，需要換更長區間或更廣的股票池才能有足夠樣本下結論。"
        ),
        "sample_note": f"共 {len(targets)} 檔目標，{len(baseline_results)} 檔有效回測結果，{len(skipped)} 檔略過（見 skipped_tickers）。",
    }
    return {"completed_at": datetime.now(timezone.utc).isoformat(), "min_score": min_score,
            "period_start": period_start, "period_end": period_end, "history_period": history_period,
            "n_tickers": len(targets), "n_valid": len(baseline_results), "skipped_tickers": skipped,
            "baseline_aggregate": b_agg, "gated_aggregate": g_agg, "comparison": comparison}
