"""
risk_manager.py — 台股波段版 v1.0
移除：EIA排程、check_earnings_risk、check_weekend_gap
修正：check_vix → check_market_circuit_breaker（大盤跌幅）
新增：check_foreign_flow、台股張數風控
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List
from config import CIRCUIT_BREAKER as CB, ACCOUNT_BALANCE_TWD, MAX_SIMULTANEOUS_POSITIONS

logger = logging.getLogger(__name__)

def check_market_circuit_breaker(market_overview: Dict) -> Dict:
    # ★ 修正：2026-08-30——twii_chg/vix 缺資料時原本直接安靜地當成 0%／20（正常），
    #   跟「大盤真的平盤、VIX真的是20」在下面的判斷完全沒有差別——但這裡是決定
    #   要不要暫停所有多單的熔斷機制，資料缺失被當成「一切正常」比報錯更危險。
    #   這裡先把「資料本身是不是真的缺失」記下來，之後如果熔斷機制在該觸發的
    #   時候沒觸發，才有辦法從 log 判斷是「市場真的沒事」還是「上游資料就是空的」。
    idx=market_overview.get("index",{}); twii=idx.get("twii",{})
    if not twii:
        logger.warning("check_market_circuit_breaker: market_overview 缺少 twii 資料，twii_chg 會當作 0% 處理")
    if not idx.get("vix"):
        logger.warning("check_market_circuit_breaker: market_overview 缺少 vix 資料，vix 會當作 20 處理")
    twii_chg=float(twii.get("chg",0) or 0); vix=float(idx.get("vix",{}).get("price",20) or 20)
    if twii_chg<=CB["twii_drop_stop"] or vix>=CB["vix_extreme"]:
        return {"triggered":True,"level":"extreme","twii_chg":twii_chg,"vix":vix,
                "message":f"🚨 大盤重挫 {twii_chg:.1f}% / VIX {vix:.0f}，暫停所有多單","action":"stop_buy"}
    if twii_chg<=CB["twii_drop_caution"] or vix>=CB["vix_high"]:
        return {"triggered":True,"level":"high","twii_chg":twii_chg,"vix":vix,
                "message":f"⚠️ 大盤偏弱 {twii_chg:.1f}%，謹慎操作","action":"reduce_confidence"}
    return {"triggered":False,"level":"normal","twii_chg":twii_chg,"vix":vix}

def check_foreign_flow(market_overview: Dict) -> Dict:
    foreign=market_overview.get("foreign",{}); nb=float(foreign.get("net_buy_twd",0) or 0)
    if nb<=CB["foreign_sell_stop"]:
        return {"triggered":True,"level":"extreme","net_buy_twd":nb,
                "message":f"🚨 外資賣超 {abs(nb)/1e8:.0f}億，暫停多單","action":"stop_buy"}
    if nb<=CB["foreign_sell_caution"]:
        return {"triggered":True,"level":"warning","net_buy_twd":nb,
                "message":f"⚠️ 外資賣超 {abs(nb)/1e8:.0f}億，降低信心度","action":"reduce_confidence"}
    if nb>=20e8:
        return {"triggered":False,"level":"positive","net_buy_twd":nb,"message":f"✅ 外資買超 {nb/1e8:.0f}億，市場偏多"}
    return {"triggered":False,"level":"normal","net_buy_twd":nb}

def check_account_requirement(ticker: str, stock_info: Dict) -> Dict:
    # ★ 修正：2026-08-30——原本假設下單一定是整張(1000股)，用 price*1000*1.1 當最低門檻。
    # 現在 calc_position_size() 已經改用零股(股數)為單位下單，不再需要湊滿一張才能進場，
    # 所以這裡的最低資金需求也不該再綁定「1張」，改成「至少買得起1股，並留一點手續費緩衝」。
    # 真正「這筆帳戶規模、這個停損距離，換算出來的股數夠不夠達到有意義的部位」這件事，
    # calc_position_size() 自己就會處理（換算後不足1股會直接回傳 shares=0、不進場），
    # 這裡只做「連1股都買不起」這種最基本的資金下限檢查。
    price=stock_info.get("close",100); min_capital=price*1.1
    if ACCOUNT_BALANCE_TWD<min_capital:
        return {"sufficient":False,"required":round(min_capital,0),"current":ACCOUNT_BALANCE_TWD,
                "message":f"⚠️ 帳戶不足買 1 股（需 TWD {min_capital:,.0f}）","warning_only":True}
    return {"sufficient":True}

_daily_loss = {"date":"","loss_twd":0.0,"signal_count":0}

def record_signal_loss(loss_twd: float):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_loss["date"]!=today:
        _daily_loss["date"]=today; _daily_loss["loss_twd"]=0.0; _daily_loss["signal_count"]=0
    if loss_twd<0: _daily_loss["loss_twd"]+=abs(loss_twd)
    _daily_loss["signal_count"]+=1

def check_daily_loss_limit() -> Dict:
    max_daily=ACCOUNT_BALANCE_TWD*0.06
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _daily_loss["date"]!=today:
        return {"exceeded":False,"today_loss":0,"max_loss":round(max_daily,0),"remaining":round(max_daily,0)}
    loss=_daily_loss["loss_twd"]
    if loss>=max_daily:
        return {"exceeded":True,"today_loss":round(loss,0),"max_loss":round(max_daily,0),
                "message":f"🔴 今日虧損 TWD {loss:,.0f}（上限 {max_daily:,.0f}），今日停止"}
    return {"exceeded":False,"today_loss":round(loss,0),"max_loss":round(max_daily,0),"remaining":round(max_daily-loss,0)}

def check_max_positions(active_signals: List[Dict]) -> Dict:
    count=len(active_signals)
    if count>=MAX_SIMULTANEOUS_POSITIONS:
        return {"exceeded":True,"count":count,"max":MAX_SIMULTANEOUS_POSITIONS,
                "message":f"⚠️ 已有 {count} 個持倉，暫停新增（上限 {MAX_SIMULTANEOUS_POSITIONS}）"}
    return {"exceeded":False,"count":count,"max":MAX_SIMULTANEOUS_POSITIONS,"remaining":MAX_SIMULTANEOUS_POSITIONS-count}

def check_margin_usage(active_signals: List[Dict]) -> Dict:
    total_risk_pct=sum(s.get("risk_pct",0) for s in active_signals)
    total_risk_twd=sum(s.get("risk_twd",0) for s in active_signals)
    total_value=sum(s.get("position_value",0) for s in active_signals)
    portfolio_pct=round(total_value/ACCOUNT_BALANCE_TWD*100,1) if ACCOUNT_BALANCE_TWD else 0
    if total_risk_pct>10:
        return {"warning":True,"total_risk_pct":round(total_risk_pct,1),"total_risk_twd":round(total_risk_twd,0),
                "portfolio_pct":portfolio_pct,"message":f"⚠️ 總風險 {total_risk_pct:.1f}%，建議控制在 6% 以下"}
    return {"warning":False,"total_risk_pct":round(total_risk_pct,1),"total_risk_twd":round(total_risk_twd,0),"portfolio_pct":portfolio_pct}

def check_price_spike(ticker: str, tf_data: Dict) -> Dict:
    closes=tf_data.get("daily",{}).get("closes",[])
    if len(closes)<3: return {"spike":False}
    rng=abs(closes[-1]-closes[-2])/closes[-2]*100 if closes[-2] else 0
    if rng>=9.5:
        return {"spike":True,"range":round(rng,2),"message":f"⚠️ {ticker} 漲跌幅 {rng:.1f}%，接近漲跌停"}
    return {"spike":False}

def run_all_checks(ticker, stock_info, tf_data, market_overview, active_signals=None) -> Dict:
    if active_signals is None: active_signals=[]
    checks={
        "market":     check_market_circuit_breaker(market_overview),
        "foreign":    check_foreign_flow(market_overview),
        "spike":      check_price_spike(ticker,tf_data),
        "account":    check_account_requirement(ticker,stock_info),
        "daily_loss": check_daily_loss_limit(),
        "max_pos":    check_max_positions(active_signals),
        "margin":     check_margin_usage(active_signals),
    }
    warnings=[]; blockers=[]; score_adj=0
    if checks["market"].get("level")=="extreme":  blockers.append(checks["market"]["message"])
    if checks["foreign"].get("level")=="extreme": blockers.append(checks["foreign"]["message"])
    if checks["daily_loss"].get("exceeded"):      blockers.append(checks["daily_loss"]["message"])
    if checks["max_pos"].get("exceeded"):         blockers.append(checks["max_pos"]["message"])
    if checks["spike"].get("spike"):              blockers.append(checks["spike"]["message"])
    if checks["market"].get("level")=="high":     warnings.append(checks["market"]["message"]); score_adj-=10
    if checks["foreign"].get("level")=="warning": warnings.append(checks["foreign"]["message"]); score_adj-=8
    if not checks["account"].get("sufficient"):   warnings.append(checks["account"]["message"]); score_adj-=20
    if checks["margin"].get("warning"):           warnings.append(checks["margin"]["message"]); score_adj-=5
    return {"status":"blocked" if blockers else "warning" if warnings else "clear",
            "warnings":warnings,"blockers":blockers,"checks":checks,"score_adj":score_adj,"can_signal":len(blockers)==0}

def get_system_status(market_overview: Dict) -> Dict:
    market_cb=check_market_circuit_breaker(market_overview)
    foreign=check_foreign_flow(market_overview)
    twii_chg=market_cb.get("twii_chg",0); vix=market_cb.get("vix",20); score=100
    if market_cb.get("level")=="extreme":   score-=50; st="大盤重挫"; cl="red"
    elif market_cb.get("level")=="high":    score-=25; st="大盤偏弱"; cl="orange"
    elif twii_chg>1.0:                      score+=10; st="大盤強勢"; cl="green"
    else:                                   st="正常";  cl="green"
    if foreign.get("level")=="extreme":    score-=20
    elif foreign.get("level")=="warning":  score-=10
    elif foreign.get("level")=="positive": score+=10
    daily=check_daily_loss_limit(); score=max(0,min(100,score))
    cat_advice={}
    for cat in ["ETF","半導體","AI概念","金融保險","航運","生技醫療"]:
        cat_advice[cat]="✅ 適合交易" if score>=70 else "⚠️ 謹慎" if score>=40 else "🔴 觀望"
    return {"env_score":score,"env_status":st,"env_color":cl,"twii_chg":twii_chg,"vix":vix,
            "can_trade":score>=50,"category_advice":cat_advice,"daily_loss":daily,"foreign_signal":foreign.get("message","")}
