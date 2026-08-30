"""
backtester.py — 台股波段版 v1.0
修正：pnl 改台股張數計算（含手續費+證交稅）
移除：外匯 pip/lot 計算、外匯黑名單
"""
import logging, time, math
from datetime import datetime, timezone
from typing import Dict, List
from config import ACCOUNT_BALANCE_TWD, SIGNAL_THRESHOLDS as THRESH, COMMISSION_RATE, TAX_RATE_SELL, SHARES_PER_LOT, MIN_COMMISSION

logger = logging.getLogger(__name__)

def calc_tw_pnl(entry, close, direction, lots):
    shares=lots*SHARES_PER_LOT
    gross=(close-entry)*shares if direction=="buy" else (entry-close)*shares
    buy_fee=max(MIN_COMMISSION,entry*shares*COMMISSION_RATE)
    sell_fee=max(MIN_COMMISSION,close*shares*COMMISSION_RATE)
    sell_tax=close*shares*TAX_RATE_SELL
    return round(gross-buy_fee-sell_fee-sell_tax,0)

def backtest_symbol_tw(ticker, initial_balance=None, min_score=None) -> Dict:
    """
    ★ 修正：改為直接呼叫 signal_engine 的 check_multi_timeframe_tw() / calc_stop_loss_tw() /
    calc_take_profits_tw() / calc_position_size()，跟 scanner.py 每天盤後真正在跑的邏輯用同一套，
    不再是 scoring_engine.calc_composite_score() 那套只有回測在用、實盤從未呼叫過的獨立評分法。
    已知限制：這裡逐根 K 棒重建的 tf_data 只有 "daily"，沒有同步重建歷史上每一根 K 棒當下的週線/
    小時線資料，所以回測出來的是策略的「日線骨架」表現，週線確認趨勢那段加分/降分在回測裡不會生效
    （但實盤在 TIMEFRAMES 修正後會生效）。要做到完全對齊，需要另外建置逐根對齊的週線/小時線歷史資料，
    這裡先讓回測至少用同一套進場評分與出場機制，而不是兩套完全不同的策略。
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
    try:
        from stock_universe import get_stock_info
        info=get_stock_info(ticker); size_cat=info.get("size_cat","中型股") if info else "中型股"
    except Exception:
        size_cat="中型股"
    n=len(closes); LOOKBACK=130; trades=[]; equity=[balance]; open_trade=None
    logger.info(f"[BT] {ticker} 開始回測，共 {n} 根日線，size_cat={size_cat}")
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
                lots=open_trade.get("lots",1)
                pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,lots)
                balance+=pnl
                trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                                "sl":sl,"tp1":tp1,"result":res,"pnl_twd":pnl,"lots":lots,
                                "pnl_pct":round(pnl/balance*100,2),"score":open_trade.get("score",0),
                                "bar_in":open_trade["bar"],"bar_out":i,"hold_days":i-open_trade["bar"]})
                open_trade=None; just_exited=True
        if open_trade is None and not just_exited:
            mtf=check_multi_timeframe_tw({"daily":wd})
            direction=mtf.get("direction","none")
            if direction!="none":
                score=mtf.get("score",0)
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
                                    if pos["lots"]>0:
                                        open_trade={"direction":direction,"fill_price":price,"sl":sl,"tp1":tp_info["tp1"],"tp2":tp_info["tp2"],
                                                    "score":score,"lots":pos["lots"],"bar":i}
        if open_trade:
            unreal_pnl=calc_tw_pnl(open_trade["fill_price"],price,open_trade["direction"],open_trade.get("lots",1))
            equity.append(balance+unreal_pnl)
        else:
            equity.append(balance)
    if open_trade:
        # ★ 修正：這裡不再額外 append 一筆 equity——上面逐K棒 mark-to-market 迴圈跑到最後一根
        # （i=n-1，price=closes[-1]）時，未平倉部位已經用同一個 cp=closes[-1] 算過浮動損益、
        # append 進 equity_curve 了，這裡只是把它「實現」成真正的 balance/trades 紀錄，數值一致，
        # 不需要重複記錄，否則 equity_curve 最後會多一個重複點。
        d=open_trade["direction"]; cp=closes[-1]; lots=open_trade.get("lots",1)
        pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,lots)
        balance+=pnl
        trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                        "result":"forced_close","pnl_twd":pnl,"lots":lots,"pnl_pct":round(pnl/balance*100,2),
                        "score":open_trade["score"],"bar_in":open_trade["bar"],"bar_out":n-1,"hold_days":n-1-open_trade["bar"]})
    metrics=calc_performance_metrics(equity,trades)
    wins=[t for t in trades if t["result"] in ("tp1","tp2")]
    losses=[t for t in trades if t["result"]=="sl"]
    init_bal=initial_balance or ACCOUNT_BALANCE_TWD
    return {"ticker":ticker,"n_bars":n,"n_trades":len(trades),"n_wins":len(wins),"n_losses":len(losses),
            "win_rate":round(len(wins)/max(len(wins)+len(losses),1)*100,1),
            "total_pnl_twd":round(sum(t["pnl_twd"] for t in trades),0),
            "initial_balance":init_bal,"final_balance":round(balance,0),
            "return_pct":round((balance-init_bal)/init_bal*100,2),
            "avg_hold_days":round(sum(t.get("hold_days",0) for t in trades)/max(len(trades),1),1),
            "sharpe":metrics.get("sharpe",0),"max_drawdown":metrics.get("max_drawdown",0),
            "calmar":metrics.get("calmar",0),"annual_return":metrics.get("annual_return",0),
            "max_consec_loss":metrics.get("max_consec_loss",0),
            "equity_curve":equity,"trades":trades[-30:],"min_score_used":min_score,
            "grade":metrics.get("sharpe_grade","—"),"dd_grade":metrics.get("dd_grade","—"),
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
                    pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,open_trade.get("lots",1))
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
            if pos["lots"]<=0: continue
            open_trade={"direction":direction,"fill_price":price,"sl":sl,"tp1":tp_info["tp1"],"score":score,"lots":pos["lots"],"bar":i}
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

def run_full_backtest_tw(tickers=None, min_score=65.0) -> Dict:
    from stock_universe import get_tw50_components
    targets=tickers or get_tw50_components(); results=[]
    logger.info(f"[BT] 批量回測 {len(targets)} 檔")
    for ticker in targets:
        try:
            r=backtest_symbol_tw(ticker,min_score=min_score)
            if "error" not in r: results.append(r)
            time.sleep(1.0)
        except Exception as e: logger.error(f"[BT] {ticker} 失敗: {e}")
    results.sort(key=lambda x:x.get("sharpe",0),reverse=True)
    return {"total":len(results),"completed_at":datetime.now(timezone.utc).isoformat(),
            "leaderboard":[{"ticker":r["ticker"],"win_rate":r["win_rate"],"sharpe":r["sharpe"],
                            "max_drawdown":r["max_drawdown"],"total_pnl_twd":r["total_pnl_twd"],
                            "return_pct":r["return_pct"],"grade":r["grade"],"n_trades":r["n_trades"]} for r in results],"details":results}
