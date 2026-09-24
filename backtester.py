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
# 過濾）做詳細回測，跟過去策略比較勝率有沒有提升」。老實說清楚這三個新功能能不能
# 被回測驗證：
#   1) K線持久化快取（ohlcv_bars）：純基礎設施，不改變任何訊號判斷邏輯，只是把
#      fetch_ohlcv() 的資料來源從「每次重新下載」換成「本地快取+增量更新」，回測
#      跟實盤用的是同一份 fetch_ohlcv()，所以這裡的改動對回測結果完全沒有影響
#      （唯一影響是回測跑起來變快，因為不用每檔都重新下載5年歷史）——不需要、
#      也沒辦法用「勝率有沒有提升」來驗證這塊，這裡不對它做任何回測。
#   2) 總經指標加權（signal_engine.generate_signal_tw 裡新增的 macro_adj 區塊）：
#      backtester.backtest_symbol_tw() 呼叫的是 check_multi_timeframe_tw()，不是
#      generate_signal_tw()，原本完全繞過這段新邏輯。這裡新增 use_macro_overlay
#      參數，在回測迴圈裡「補」上這段調整——但只補得起大盤漲跌（TWII）這一項，
#      因為：
#        - 外資買賣超的歷史需要逐日呼叫 TWSE T86 API，這個專案自己的文件已經記錄
#          這組 API 很容易逾時/不穩定，硬要回測成本高、資料品質沒把握，這裡不做。
#        - 融資餘額的歷史數列是今天才剛開始持久化（state_store 的
#          margin_balance_history），完全沒有回測用得到的歷史資料，只能往前累積、
#          之後才有辦法做「事後驗證」，這裡也不做。
#      所以這個「總經回測」只是「大盤熔斷（TWII）這一項有沒有效」的部分驗證，
#      不是完整三項總經指標的驗證——這點會誠實告知使用者，不誇大回測涵蓋範圍。
#   3) 個股基本面月營收硬性過濾：fetch_monthly_revenue_map() 只能拿到「當下這個月」
#      全市場的營收，系統裡沒有任何歷史月營收時間序列，沒辦法回頭重建「這檔股票在
#      2024年3月那天，月營收年增率是多少」——這裡沒有辦法回測，只能等之後每個月
#      系統自己跑出來的資料累積成歷史數列，才有辦法回頭驗證。
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

def _macro_score_adj(day_date: Optional[str], twii_chg_map: Dict[str, float]) -> int:
    """複製 signal_engine.generate_signal_tw() 裡那段 macro_adj 的邏輯，但只用
    大盤(TWII)這一項（見上方說明：外資、融資這兩項回測沒有可用的歷史資料）。
    跟 risk_manager.check_market_circuit_breaker() 用同一組門檻(CB)，"high"
    等級一樣扣10分，維持跟實盤一致的規則，不是另外發明一套。"""
    if not day_date or day_date not in twii_chg_map:
        return 0
    twii_chg = twii_chg_map[day_date]
    if twii_chg <= CB["twii_drop_caution"]:
        return -10
    return 0

# ★ 修正：2026-08-30（第二輪）——跟 signal_engine.calc_position_size() 的修正配套：不再假設倉位
# 一定是「張」(1000股)的整數倍，改直接吃股數。calc_tw_pnl 的 shares 參數現在就是股數本身，
# 不用再乘 SHARES_PER_LOT。
def calc_tw_pnl(entry, close, direction, shares):
    gross=(close-entry)*shares if direction=="buy" else (entry-close)*shares
    buy_fee=max(MIN_COMMISSION,entry*shares*COMMISSION_RATE)
    sell_fee=max(MIN_COMMISSION,close*shares*COMMISSION_RATE)
    sell_tax=close*shares*TAX_RATE_SELL
    return round(gross-buy_fee-sell_fee-sell_tax,0)

def backtest_symbol_tw(ticker, initial_balance=None, min_score=None, use_macro_overlay=False) -> Dict:
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
    logger.info(f"[BT] {ticker} 開始回測，共 {n} 根日線，size_cat={size_cat}，use_macro_overlay={use_macro_overlay}")
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
            mtf=check_multi_timeframe_tw(tf_data_bt)
            direction=mtf.get("direction","none")
            if direction!="none":
                score=mtf.get("score",0)
                if use_macro_overlay:
                    macro_adj=_macro_score_adj(day_date, twii_chg_map)
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
                                if tp_info["rr1"]>=THRESH["min_rr"]:
                                    pos=calc_position_size(price,sl,balance=balance,size_cat=size_cat)
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
            "use_macro_overlay":use_macro_overlay,
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
            pos=calc_position_size(price,sl,balance=balance,size_cat=size_cat)
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

def run_full_backtest_tw(tickers=None, min_score=65.0, progress_cb=None, use_macro_overlay=False) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components(); results=[]
    logger.info(f"[BT] 批量回測 {len(targets)} 檔，use_macro_overlay={use_macro_overlay}")
    for idx,ticker in enumerate(targets):
        try:
            r=backtest_symbol_tw(ticker,min_score=min_score,use_macro_overlay=use_macro_overlay)
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
            "use_macro_overlay":use_macro_overlay,
            "overall_win_rate":overall_win_rate,"n_trades_total":n_trades_total,
            "avg_sharpe":avg_sharpe,"avg_return_pct":avg_return_pct,
            "by_direction_aggregate":agg,
            "leaderboard":[{"ticker":r["ticker"],"win_rate":r["win_rate"],"sharpe":r["sharpe"],
                            "max_drawdown":r["max_drawdown"],"total_pnl_twd":r["total_pnl_twd"],
                            "return_pct":r["return_pct"],"grade":r["grade"],"n_trades":r["n_trades"],
                            "by_direction":r.get("by_direction",{})} for r in results],"details":results}


# ★ 新增：2026-09-24——使用者這次明確要求「用新增的東西做詳細回測，跟過去策略
# 比較勝率/準確度有沒有提升」。這裡把「有沒有大盤總經加權」的兩次全市場回測
# 包成一個函式，跑兩輪 run_full_backtest_tw()（一輪 use_macro_overlay=False
# 當作「過去策略」基準，一輪 True 當作「新策略」），回傳兩邊的整體統計＋差異，
# 讓使用者不用自己手動比對兩次個別的回測結果。
def run_comparison_backtest_tw(tickers=None, min_score=65.0, progress_cb=None) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components()
    def _cb_baseline(done,total,ticker):
        if progress_cb: progress_cb(done,total*2,f"[基準/舊策略] {ticker}")
    def _cb_overlay(done,total,ticker):
        if progress_cb: progress_cb(total+done,total*2,f"[新策略/總經加權] {ticker}")
    baseline=run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb_baseline,use_macro_overlay=False)
    overlay =run_full_backtest_tw(targets,min_score=min_score,progress_cb=_cb_overlay, use_macro_overlay=True)
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
        "scope_note":("此比較僅涵蓋「大盤(TWII)熔斷」這一項總經指標的回測驗證——"
                       "K線持久化快取是純基礎設施不影響訊號邏輯（不需要回測）；"
                       "外資買賣超歷史資料需逐日查詢TWSE API、成本高且不穩定，此處未回測；"
                       "融資餘額與個股月營收歷史數列今天才剛開始累積，系統中尚無可回測的歷史資料，"
                       "僅能持續累積後續才能做事後驗證。"),
    }
    return {"completed_at":datetime.now(timezone.utc).isoformat(),"min_score":min_score,
            "n_tickers":len(targets),"baseline":baseline,"overlay":overlay,"comparison":comparison}
