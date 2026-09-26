"""
backtester.py — 台股波段版 v1.0
修正：pnl 改台股股數計算（含手續費+證交稅）
移除：外匯 pip/lot 計算、外匯黑名單
"""
import logging, time, math, bisect
from datetime import datetime, timezone
from typing import Dict, List, Optional
from config import ACCOUNT_BALANCE_TWD, SIGNAL_THRESHOLDS as THRESH, COMMISSION_RATE, TAX_RATE_SELL, SHARES_PER_LOT, MIN_COMMISSION, CIRCUIT_BREAKER as CB

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

# ★ 修正：2026-08-30（第二輪）——跟 signal_engine.calc_position_size() 的修正配套：不再假設倉位
# 一定是「張」(1000股)的整數倍，改直接吃股數。calc_tw_pnl 的 shares 參數現在就是股數本身，
# 不用再乘 SHARES_PER_LOT。
def calc_tw_pnl(entry, close, direction, shares):
    gross=(close-entry)*shares if direction=="buy" else (entry-close)*shares
    buy_fee=max(MIN_COMMISSION,entry*shares*COMMISSION_RATE)
    sell_fee=max(MIN_COMMISSION,close*shares*COMMISSION_RATE)
    sell_tax=close*shares*TAX_RATE_SELL
    return round(gross-buy_fee-sell_fee-sell_tax,0)

def backtest_symbol_tw(ticker, initial_balance=None, min_score=None, use_macro_overlay=False, use_fundamentals_filter=False, disabled_factors=None) -> Dict:
    """
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
        """回傳「這根日K當下」看得到的週線切片（週線起始日<=day_date的所有週線K棒）。
        資料不足20根週線就回傳None，交給 check_multi_timeframe_tw 內部的valid檢查決定
        要不要採用，行為上等同於實盤時週線資料不足的狀況。"""
        if not w_dates or not day_date: return None
        w_idx=bisect.bisect_right(w_dates, day_date)
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
    logger.info(f"[BT] {ticker} 開始回測，共 {n} 根日線，size_cat={size_cat}，"
                f"use_macro_overlay={use_macro_overlay}，use_fundamentals_filter={use_fundamentals_filter}")
    # ★ 修正：2026-08-30——投資人報告核對數字時發現 Sharpe/最大回撤嚴重失真的根因：equity_curve
    #   原本只在「每次平倉當下」才記一筆，不是每根K棒都記。calc_performance_metrics() 卻把這條
    #   equity_curve 相鄰兩點的報酬率當「逐日報酬」處理、年化時乘上 sqrt(252)——但兩個平倉點之間
    #   往往隔了好幾天甚至好幾週，實際上是「逐筆交易報酬」被誤當成「逐日報酬」年化，交易筆數越少
    #   （min_score≥65 門檻本來就選得嚴）灌水越誇張，這是 Sharpe 動輒破百的直接原因；同時最大回撤
    #   也因此只看得到「已實現」損益的高低點，完全看不到持倉中途尚未停損/停利、帳面上一度虧很多的
    #   情況。改成不管有沒有平倉，「每一根K棒」都用當下收盤價把未平倉部位的浮動損益 mark-to-market
    #   計入 equity，讓 equity_curve 變成真正的逐日序列，calc_performance_metrics() 不用改，
    #   sqrt(252) 年化、回撤計算就都會是對的。
    for i in range(LOOKBACK,n):
        wd={"closes":closes[:i],"highs":highs[:i],"lows":lows[:i],"opens":opens[:i],"volumes":volumes[:i]}
        day_date=dates[i] if i<len(dates) else None
        price=closes[i]
        just_exited=False
        if open_trade:
            d=open_trade["direction"]; sl=open_trade["sl"]; tp1=open_trade["tp1"]; tp2=open_trade["tp2"]
            hit_sl=(d=="buy" and lows[i]<=sl) or (d=="sell" and highs[i]>=sl)
            hit_tp2=(d=="buy" and highs[i]>=tp2) or (d=="sell" and lows[i]<=tp2)
            hit_tp1=(d=="buy" and highs[i]>=tp1) or (d=="sell" and lows[i]<=tp1)
            if hit_sl or hit_tp2 or hit_tp1:
                if hit_sl: res,cp="sl",sl
                elif hit_tp2: res,cp="tp2",tp2
                else: res,cp="tp1",tp1
                shares=open_trade.get("shares",1)
                pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,shares)
                entry_balance=balance
                balance+=pnl
                trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                                "sl":sl,"tp1":tp1,"result":res,"pnl_twd":pnl,"shares":shares,
                                "pnl_pct":round(pnl/entry_balance*100,2),"score":open_trade.get("score",0),
                                "bar_in":open_trade["bar"],"bar_out":i,"hold_days":i-open_trade["bar"]})
                open_trade=None; just_exited=True
        if open_trade is None and not just_exited:
            tf_data_bt={"daily":wd}
            w_slice=_weekly_asof(day_date)
            if w_slice: tf_data_bt["weekly"]=w_slice
            mtf=check_multi_timeframe_tw(tf_data_bt, disabled_factors=disabled_factors)
            direction=mtf.get("direction","none")
            fund_blocked=False
            if direction!="none" and use_fundamentals_filter and day_date:
                fund_chk=check_fundamental_hard_filter_asof(fund_code, day_date)
                fund_blocked=fund_chk.get("blocked", False)
            if direction!="none" and not fund_blocked:
                score=mtf.get("score",0)
                if use_macro_overlay:
                    macro_adj=_macro_score_adj(day_date, twii_chg_map, margin_chg_map)
                    if macro_adj:
                        score=max(0,score+macro_adj)
                if score>=min_score:
                    adx_val=mtf.get("adx_value",0)
                    if adx_val>=THRESH["min_adx"]:
                        vol_ratio=mtf.get("vol_ratio",1.0)
                        if not(vol_ratio<THRESH["min_vol_ratio"] and score<75):
                            daily_ind=mtf.get("entry_indicators",{})
                            atr=daily_ind.get("atr",{}).get("value",0) or price*0.02
                            if atr:
                                low_5d=min(lows[max(0,i-5):i]) if i>=5 else None
                                high_5d=max(highs[max(0,i-5):i]) if i>=5 else None
                                sl=calc_stop_loss_tw(direction,price,atr,daily_ind,size_cat,low_5d,high_5d)
                                tp_info=calc_take_profits_tw(direction,price,sl,size_cat)
                                # ★ 修正：2026-09-26——稽核發現回測跟實盤在進場過濾條件上有落差：
                                # signal_engine.generate_signal_tw() 有「停損距離>12%就跳過（追高/
                                # 已離結構性支撐太遠）」的過濾（見該檔案該函式），回測這裡完全沒有
                                # 套用，導致回測會納入實盤根本不會進場的訊號，讓回測結果失真。這裡
                                # 補上同一條件，跟實盤邏輯一致。同時 calc_position_size() 補上
                                # avg_volume_lots，跟實盤一樣用該股均量做流動性上限，不然回測部位
                                # 大小可能比實盤實際可成交的量還大。
                                sl_dist_pct=abs(price-sl)/price*100 if price else 0
                                if tp_info["rr1"]>=THRESH["min_rr"] and sl_dist_pct<=12:
                                    pos=calc_position_size(price,sl,balance=balance,size_cat=size_cat,
                                                            avg_volume_lots=(info.get("volume_lots") if info else None))
                                    if pos["shares"]>0:
                                        open_trade={"direction":direction,"fill_price":price,"sl":sl,"tp1":tp_info["tp1"],"tp2":tp_info["tp2"],
                                                    "score":score,"shares":pos["shares"],"bar":i}
        if open_trade:
            unreal_pnl=calc_tw_pnl(open_trade["fill_price"],price,open_trade["direction"],open_trade.get("shares",1))
            equity.append(balance+unreal_pnl)
        else:
            equity.append(balance)
    if open_trade:
        # ★ 修正：這裡不再額外 append 一筆 equity——上面逐K棒 mark-to-market 迴圈跑到最後一根
        # （i=n-1，price=closes[-1]）時，未平倉部位已經用同一個 cp=closes[-1] 算過浮動損益、
        # append 進 equity_curve 了，這裡只是把它「實現」成真正的 balance/trades 紀錄，數值一致，
        # 不需要重複記錄，否則 equity_curve 最後會多一個重複點。
        d=open_trade["direction"]; cp=closes[-1]; shares=open_trade.get("shares",1)
        pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,shares)
        balance+=pnl
        trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                        "result":"forced_close","pnl_twd":pnl,"shares":shares,"pnl_pct":round(pnl/balance*100,2),
                        "score":open_trade["score"],"bar_in":open_trade["bar"],"bar_out":n-1,"hold_days":n-1-open_trade["bar"]})
    metrics=calc_performance_metrics(equity,trades)
    wins=[t for t in trades if t["result"] in ("tp1","tp2")]
    losses=[t for t in trades if t["result"]=="sl"]
    init_bal=initial_balance or ACCOUNT_BALANCE_TWD
    # ★ 新增：2026-09-19——使用者反映策略不只做多、放空也是策略的一部分，要求
    # 詳細檢測放空這邊是否可行。原本這裡只回傳「不分方向」混在一起的勝率/損益，
    # 沒辦法回答「做多跟做空分開看，表現是不是有系統性差異」這個問題。這裡在
    # truncate成trades[-30:]之前，先用完整的trades（不是截斷後的30筆）分別
    # 算一份buy/sell各自的勝率、筆數、損益，讓run_full_backtest_tw()可以在
    # 多檔加總時，把兩個方向的統計分開呈現，而不是混在一起看不出方向性的差異。
    def _dir_stats(direction):
        d_trades=[t for t in trades if t["direction"]==direction]
        d_wins=[t for t in d_trades if t["result"] in ("tp1","tp2")]
        d_losses=[t for t in d_trades if t["result"]=="sl"]
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
    try:
        from stock_universe import get_stock_info
        info=get_stock_info(ticker); size_cat=info.get("size_cat","中型股") if info else "中型股"
    except Exception:
        size_cat="中型股"
    n=len(closes); windows=[]; start=train_bars
    while start+test_bars<=n: windows.append((start-train_bars,start,start+test_bars)); start+=test_bars
    all_test_trades=[]; window_results=[]
    for win_idx,(tr_start,tr_end,te_end) in enumerate(windows):
        test_trades=[]; open_trade=None; balance=ACCOUNT_BALANCE_TWD
        for i in range(tr_end,te_end):
            wd={"closes":closes[tr_start:i],"highs":highs[tr_start:i],"lows":lows[tr_start:i],"opens":opens[tr_start:i],"volumes":volumes[tr_start:i]}
            if len(wd["closes"])<130: continue
            price=closes[i]
            if open_trade:
                d=open_trade["direction"]; sl=open_trade["sl"]; tp1=open_trade["tp1"]
                hit_sl=(d=="buy" and lows[i]<=sl) or (d=="sell" and highs[i]>=sl)
                hit_tp1=(d=="buy" and highs[i]>=tp1) or (d=="sell" and lows[i]<=tp1)
                if hit_sl or hit_tp1:
                    res="tp1" if hit_tp1 else "sl"; cp=tp1 if hit_tp1 else sl
                    pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,open_trade.get("shares",1))
                    balance+=pnl; t={"result":res,"pnl_twd":pnl,"score":open_trade["score"]}
                    test_trades.append(t); all_test_trades.append(t); open_trade=None; continue
            if open_trade: continue
            mtf=check_multi_timeframe_tw({"daily":wd})
            direction=mtf.get("direction","none")
            if direction=="none": continue
            score=mtf.get("score",0)
            if score<min_score: continue
            if mtf.get("adx_value",0)<THRESH["min_adx"]: continue
            daily_ind=mtf.get("entry_indicators",{})
            atr=daily_ind.get("atr",{}).get("value",0) or price*0.02
            if not atr: continue
            local_i=i-tr_start
            low_5d=min(lows[max(tr_start,i-5):i]) if local_i>=5 else None
            high_5d=max(highs[max(tr_start,i-5):i]) if local_i>=5 else None
            sl=calc_stop_loss_tw(direction,price,atr,daily_ind,size_cat,low_5d,high_5d)
            tp_info=calc_take_profits_tw(direction,price,sl,size_cat)
            if tp_info["rr1"]<THRESH["min_rr"]: continue
            # ★ 修正：2026-09-26——同 backtest_symbol_tw() 補上的兩項，跟實盤 generate_signal_tw()
            # 對齊：停損距離>12%（追高過濾）就跳過；calc_position_size() 補上 avg_volume_lots
            # 流動性上限。
            sl_dist_pct=abs(price-sl)/price*100 if price else 0
            if sl_dist_pct>12: continue
            pos=calc_position_size(price,sl,balance=balance,size_cat=size_cat,
                                    avg_volume_lots=(info.get("volume_lots") if info else None))
            if pos["shares"]<=0: continue
            open_trade={"direction":direction,"fill_price":price,"sl":sl,"tp1":tp_info["tp1"],"score":score,"shares":pos["shares"],"bar":i}
        wins_w=len([t for t in test_trades if t["result"]=="tp1"])
        window_results.append({"window":win_idx+1,"n_trades":len(test_trades),
                                "win_rate":round(wins_w/max(len(test_trades),1)*100,1),
                                "total_pnl":round(sum(t["pnl_twd"] for t in test_trades),0)})
    all_wins=len([t for t in all_test_trades if t["result"]=="tp1"])
    return {"ticker":ticker,"method":"walk_forward","n_windows":len(windows),"train_bars":train_bars,"test_bars":test_bars,
            "total_trades":len(all_test_trades),"overall_win_rate":round(all_wins/max(len(all_test_trades),1)*100,1),
            "total_pnl_twd":round(sum(t["pnl_twd"] for t in all_test_trades),0),
            "window_results":window_results,
            "stability":round(sum(1 for w in window_results if w["win_rate"]>=50)/max(len(window_results),1)*100,1),
            "completed_at":datetime.now(timezone.utc).isoformat()}

def run_full_backtest_tw(tickers=None, min_score=65.0, progress_cb=None, use_macro_overlay=False, use_fundamentals_filter=False, disabled_factors=None) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components(); results=[]
    logger.info(f"[BT] 批量回測 {len(targets)} 檔，use_macro_overlay={use_macro_overlay}，use_fundamentals_filter={use_fundamentals_filter}，disabled_factors={disabled_factors}")
    for idx,ticker in enumerate(targets):
        try:
            r=backtest_symbol_tw(ticker,min_score=min_score,use_macro_overlay=use_macro_overlay,use_fundamentals_filter=use_fundamentals_filter,disabled_factors=disabled_factors)
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
        if not w_dates or not day_date: return None
        w_idx=bisect.bisect_right(w_dates, day_date)
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
    for i in range(LOOKBACK,n):
        wd={"closes":closes[:i],"highs":highs[:i],"lows":lows[:i],"opens":opens[:i],"volumes":volumes[:i]}
        day_date=dates[i] if i<len(dates) else None
        price=closes[i]
        realized_pnl_today=0.0
        if open_trade:
            # ★ 同K棒觸價順序假設（2026-09-26 補上文件化，邏輯本身未變）：
            # 日K資料無法知道同一天內「先漲到TP還是先跌到停損」的真實順序，
            # 這裡採保守假設——同一根K棒只要停損價與任何TP價「同時」滿足觸價
            # 條件，一律優先判定停損成交（下方 hit_stop 檢查在 TP 迴圈之前，
            # 且用 if/else 互斥），不會有一天內同時記錄「先出停損又出TP」的
            # 矛盾結果。這會讓回測結果偏向低估獲利/高估虧損（比真實情況更保守），
            # 但避免了「同一天同時停損又停利」這種不可能發生於真實單一部位的
            # 假象。日後若要提高精度，應改用小時線或更細週期資料判斷真實順序，
            # 而不是放寬這個假設。
            d=open_trade["direction"]; cur_sl=open_trade["current_sl"]
            hit_stop=(d=="buy" and lows[i]<=cur_sl) or (d=="sell" and highs[i]>=cur_sl)
            if hit_stop and open_trade["shares_remaining"]>0:
                shares=open_trade["shares_remaining"]
                pnl=calc_tw_pnl(open_trade["fill_price"],cur_sl,d,shares)
                balance+=pnl; realized_pnl_today+=pnl
                open_trade["legs"].append({"stage":"stop","price":cur_sl,"shares":shares,"pnl_twd":pnl})
                open_trade["shares_remaining"]=0
            else:
                for stage,target_key in (("tp1","tp1"),("tp2","tp2"),("tp3","tp3")):
                    if open_trade["shares_remaining"]<=0: break
                    if open_trade["leg_done"][stage]: continue
                    leg_shares=open_trade["leg_shares"][stage]
                    if leg_shares<=0: continue
                    tgt=open_trade[target_key]
                    hit=(d=="buy" and highs[i]>=tgt) or (d=="sell" and lows[i]<=tgt)
                    if not hit: break  # 未碰到較近的目標，後面更遠的目標當天不可能碰到
                    pnl=calc_tw_pnl(open_trade["fill_price"],tgt,d,leg_shares)
                    balance+=pnl; realized_pnl_today+=pnl
                    open_trade["legs"].append({"stage":stage,"price":tgt,"shares":leg_shares,"pnl_twd":pnl})
                    open_trade["leg_done"][stage]=True
                    open_trade["shares_remaining"]-=leg_shares
                    if stage=="tp1": open_trade["current_sl"]=open_trade["fill_price"]      # 移至保本
                    elif stage=="tp2": open_trade["current_sl"]=open_trade["tp1"]           # 移至TP1鎖利
            if open_trade["shares_remaining"]<=0:
                legs=open_trade["legs"]
                total_pnl=sum(l["pnl_twd"] for l in legs)
                result="+".join(l["stage"] for l in legs) or "flat"
                trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],
                                "result":result,"pnl_twd":total_pnl,"shares":open_trade["shares_total"],
                                "pnl_pct":round(total_pnl/max(balance-total_pnl,1)*100,2),
                                "score":open_trade.get("score",0),"legs":legs,
                                "n_legs_hit":sum(1 for l in legs if l["stage"]!="stop"),
                                "bar_in":open_trade["bar"],"bar_out":i,"hold_days":i-open_trade["bar"]})
                open_trade=None
        if open_trade is None:
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
                            atr=daily_ind.get("atr",{}).get("value",0) or price*0.02
                            if atr:
                                low_5d=min(lows[max(0,i-5):i]) if i>=5 else None
                                high_5d=max(highs[max(0,i-5):i]) if i>=5 else None
                                sl=calc_stop_loss_tw(direction,price,atr,daily_ind,size_cat,low_5d,high_5d)
                                tp_info=calc_take_profits_tw(direction,price,sl,size_cat)
                                # ★ 修正：2026-09-26——同 backtest_symbol_tw() 補上的兩項，跟實盤
                                # generate_signal_tw() 對齊：停損距離>12%（追高過濾）就跳過；
                                # calc_position_size() 補上 avg_volume_lots 流動性上限。
                                sl_dist_pct=abs(price-sl)/price*100 if price else 0
                                if tp_info["rr1"]>=THRESH["min_rr"] and sl_dist_pct<=12:
                                    pos=calc_position_size(price,sl,balance=balance,size_cat=size_cat,
                                                            avg_volume_lots=(info.get("volume_lots") if info else None))
                                    shares_total=pos["shares"]
                                    if shares_total>0:
                                        leg1=shares_total//3; leg2=shares_total//3; leg3=shares_total-leg1-leg2
                                        if leg1<=0:  # 部位太小無法真正分三段，退化為單段（全部併入tp3段）
                                            leg1=0; leg2=0; leg3=shares_total
                                        open_trade={"direction":direction,"fill_price":price,
                                                    "tp1":tp_info["tp1"],"tp2":tp_info["tp2"],"tp3":tp_info["tp3"],
                                                    "current_sl":sl,"score":score,
                                                    "shares_total":shares_total,"shares_remaining":shares_total,
                                                    "leg_shares":{"tp1":leg1,"tp2":leg2,"tp3":leg3},
                                                    "leg_done":{"tp1":False,"tp2":False,"tp3":False},
                                                    "legs":[],"bar":i}
        if open_trade and open_trade["shares_remaining"]>0:
            unreal=calc_tw_pnl(open_trade["fill_price"],price,open_trade["direction"],open_trade["shares_remaining"])
            equity.append(balance+unreal)
        else:
            equity.append(balance)
    if open_trade and open_trade["shares_remaining"]>0:
        d=open_trade["direction"]; cp=closes[-1]; shares=open_trade["shares_remaining"]
        pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,shares)
        balance+=pnl
        legs=open_trade["legs"]+[{"stage":"forced_close","price":cp,"shares":shares,"pnl_twd":pnl}]
        total_pnl=sum(l["pnl_twd"] for l in legs)
        trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],
                        "result":"+".join(l["stage"] for l in legs),"pnl_twd":total_pnl,
                        "shares":open_trade["shares_total"],"pnl_pct":round(total_pnl/max(balance,1)*100,2),
                        "score":open_trade.get("score",0),"legs":legs,
                        "n_legs_hit":sum(1 for l in legs if l["stage"] not in ("stop","forced_close")),
                        "bar_in":open_trade["bar"],"bar_out":n-1,"hold_days":n-1-open_trade["bar"]})
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
