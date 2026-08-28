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

def calc_lots_by_risk(price, sl, balance, risk_pct=2.0):
    max_risk=balance*risk_pct/100; sl_dist=abs(price-sl)
    if sl_dist<=0: return 1
    risk_per_lot=sl_dist*SHARES_PER_LOT
    raw_lots=max_risk/risk_per_lot
    max_by_cap=(balance*0.30)/(price*SHARES_PER_LOT)
    return max(1,min(10,int(min(raw_lots,max_by_cap))))

def backtest_symbol_tw(ticker, initial_balance=None, min_score=65.0) -> Dict:
    from data_fetcher   import fetch_ohlcv
    from indicators     import calc_all_indicators
    from scoring_engine import calc_composite_score, calc_performance_metrics
    from state_store    import store
    balance=initial_balance or ACCOUNT_BALANCE_TWD
    data=fetch_ohlcv(ticker,"daily")
    if not data or len(data.get("closes",[]))<60:
        return {"error":f"{ticker} 歷史數據不足（需60根日線）"}
    closes=data["closes"]; highs=data["highs"]; lows=data["lows"]; opens=data["opens"]; volumes=data["volumes"]
    n=len(closes); LOOKBACK=130; trades=[]; equity=[balance]; open_trade=None
    logger.info(f"[BT] {ticker} 開始回測，共 {n} 根日線")
    for i in range(LOOKBACK,n):
        wd={"closes":closes[:i],"highs":highs[:i],"lows":lows[:i],"opens":opens[:i],"volumes":volumes[:i]}
        ind=calc_all_indicators(wd)
        if not ind.get("valid"): continue
        price=closes[i]
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
                balance+=pnl; equity.append(balance)
                trades.append({"ticker":ticker,"direction":d,"entry":open_trade["fill_price"],"close":cp,
                                "sl":sl,"tp1":tp1,"result":res,"pnl_twd":pnl,"lots":lots,
                                "pnl_pct":round(pnl/balance*100,2),"score":open_trade.get("score",0),
                                "bar_in":open_trade["bar"],"bar_out":i,"hold_days":i-open_trade["bar"]})
                open_trade=None; continue
        if open_trade: continue
        direction=("buy" if ind.get("overall_bias","").startswith("bullish") else
                   "sell" if ind.get("overall_bias","").startswith("bearish") else None)
        if not direction: continue
        sc=calc_composite_score(ind,direction)
        if sc["composite"]<min_score: continue
        if ind.get("adx_value",0)<18: continue
        if ind.get("volume",{}).get("ratio",1.0)<1.2 and sc["composite"]<75: continue
        atr=ind.get("atr",{}).get("value",0)
        if not atr: continue
        sl_mult=1.8; tp1_rr=2.0; tp2_rr=3.5
        if direction=="buy":
            sl=price-atr*sl_mult; tp1=price+atr*sl_mult*tp1_rr; tp2=price+atr*sl_mult*tp2_rr
        else:
            sl=price+atr*sl_mult; tp1=price-atr*sl_mult*tp1_rr; tp2=price-atr*sl_mult*tp2_rr
        risk=abs(price-sl); reward=abs(tp1-price)
        if risk<=0 or reward/risk<THRESH["min_rr"]: continue
        lots=calc_lots_by_risk(price,sl,balance)
        open_trade={"direction":direction,"fill_price":price,"sl":sl,"tp1":tp1,"tp2":tp2,
                    "score":sc["composite"],"lots":lots,"bar":i}
    if open_trade:
        d=open_trade["direction"]; cp=closes[-1]; lots=open_trade.get("lots",1)
        pnl=calc_tw_pnl(open_trade["fill_price"],cp,d,lots)
        balance+=pnl; equity.append(balance)
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

def walk_forward_backtest_tw(ticker, train_bars=150, test_bars=30, min_score=65.0) -> Dict:
    from data_fetcher   import fetch_ohlcv
    from indicators     import calc_all_indicators
    from scoring_engine import calc_composite_score
    data=fetch_ohlcv(ticker,"daily")
    if not data or len(data.get("closes",[]))<train_bars+test_bars:
        return {"error":"數據不足以進行 Walk-Forward"}
    closes=data["closes"]; highs=data["highs"]; lows=data["lows"]; opens=data["opens"]; volumes=data["volumes"]
    n=len(closes); windows=[]; start=train_bars
    while start+test_bars<=n: windows.append((start-train_bars,start,start+test_bars)); start+=test_bars
    all_test_trades=[]; window_results=[]
    for win_idx,(tr_start,tr_end,te_end) in enumerate(windows):
        test_trades=[]; open_trade=None; balance=ACCOUNT_BALANCE_TWD
        for i in range(tr_end,te_end):
            wd={"closes":closes[tr_start:i],"highs":highs[tr_start:i],"lows":lows[tr_start:i],"opens":opens[tr_start:i],"volumes":volumes[tr_start:i]}
            if len(wd["closes"])<130: continue
            ind=calc_all_indicators(wd)
            if not ind.get("valid"): continue
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
            direction=("buy" if ind.get("overall_bias","").startswith("bullish") else
                       "sell" if ind.get("overall_bias","").startswith("bearish") else None)
            if not direction: continue
            sc=calc_composite_score(ind,direction)
            if sc["composite"]<min_score: continue
            atr=ind.get("atr",{}).get("value",0)
            if not atr: continue
            sl=(price-atr*1.8) if direction=="buy" else (price+atr*1.8)
            tp1=(price+atr*3.6) if direction=="buy" else (price-atr*3.6)
            lots=calc_lots_by_risk(price,sl,balance)
            open_trade={"direction":direction,"fill_price":price,"sl":sl,"tp1":tp1,"score":sc["composite"],"lots":lots,"bar":i}
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
