"""
scoring_engine.py — 台股波段版 v1.0
移除：SLIPPAGE_MODEL、apply_slippage、check_portfolio_correlation
修正：detect_regime 改用 TWII
"""
import math, logging
from typing import List, Dict
from config import ACCOUNT_BALANCE_TWD, CB

logger = logging.getLogger(__name__)

SCORE_WEIGHTS = {
    "ema_alignment":  {"weight":20,"desc":"EMA排列"},
    "rsi_zone":       {"weight":12,"desc":"RSI區間"},
    "macd_momentum":  {"weight":12,"desc":"MACD動能"},
    "adx_strength":   {"weight":15,"desc":"ADX趨勢強度"},
    "bb_position":    {"weight":8, "desc":"布林帶位置"},
    "volume_confirm": {"weight":10,"desc":"成交量確認"},
    "mtf_resonance":  {"weight":15,"desc":"多時框共振"},
    "candlestick":    {"weight":5, "desc":"K線形態"},
    "sr_proximity":   {"weight":3, "desc":"支撐壓力位"},
}

def score_ema(indicators, direction):
    ema=indicators.get("ema",{})
    if not ema.get("valid"): return 0.3
    bias=ema.get("bias","neutral")
    base={"bullish":{"buy":0.8,"sell":0.2},"bearish":{"buy":0.2,"sell":0.8},"neutral":{"buy":0.4,"sell":0.4}}.get(bias,{"buy":0.4,"sell":0.4}).get(direction,0.4)
    if "完美" in ema.get("alignment",""): base=min(1.0,base+0.15)
    if direction=="buy"  and "金叉" in str(ema.get("cross","")): base=min(1.0,base+0.1)
    if direction=="sell" and "死叉" in str(ema.get("cross","")): base=min(1.0,base+0.1)
    e120=ema.get("e120") or ema.get("e_trend"); price=indicators.get("current_price",0)
    if e120 and price:
        if direction=="buy"  and price>e120*1.005: base=min(1.0,base+0.05)
        if direction=="sell" and price<e120*0.995: base=min(1.0,base+0.05)
    return base

def score_rsi(indicators, direction):
    rsi=indicators.get("rsi",{})
    if not rsi.get("valid"): return 0.3
    val=rsi.get("value",50)
    if direction=="buy":
        if 45<=val<=65: return 0.9
        elif 35<=val<45: return 0.7
        elif 65<val<=70: return 0.6
        elif val>70: return 0.2
        else: return 0.5
    else:
        if 35<=val<=55: return 0.9
        elif 55<val<=65: return 0.7
        elif 30<=val<35: return 0.6
        elif val<30: return 0.2
        else: return 0.5

def score_macd(indicators, direction):
    macd=indicators.get("macd",{})
    if not macd.get("valid"): return 0.3
    bias=macd.get("bias","neutral"); growing=macd.get("hist_growing",False); cross=macd.get("cross","")
    base=0.3
    if direction=="buy":
        if "bullish" in bias: base=0.75
        if growing and "bullish" in bias: base=0.85
        if cross=="MACD金叉": base=min(1.0,base+0.1)
    else:
        if "bearish" in bias: base=0.75
        if not growing and "bearish" in bias: base=0.85
        if cross=="MACD死叉": base=min(1.0,base+0.1)
    return base

def score_adx(indicators):
    v=indicators.get("adx_value",0)
    if v==0: return 0.1
    if v>=40: return 1.0
    elif v>=30: return 0.85
    elif v>=25: return 0.70
    elif v>=20: return 0.50
    elif v>=15: return 0.35
    else: return 0.15

def score_bb(indicators, direction):
    bb=indicators.get("bb",{})
    if not bb.get("valid"): return 0.4
    pos=bb.get("position","")
    if direction=="buy":
        return {"中軌上方":0.8,"突破上軌":0.6,"中軌下方":0.3,"突破下軌":0.2}.get(pos,0.4)
    else:
        return {"中軌下方":0.8,"突破下軌":0.6,"中軌上方":0.3,"突破上軌":0.2}.get(pos,0.4)

def score_volume(indicators):
    vol=indicators.get("volume",{})
    if not vol.get("valid"): return 0.3
    r=vol.get("ratio",1.0)
    if r>=3.0: return 1.0
    elif r>=2.0: return 0.95
    elif r>=1.5: return 0.80
    elif r>=1.2: return 0.65
    elif r>=1.0: return 0.50
    elif r>=0.7: return 0.35
    else: return 0.15

def score_mtf(bull_tfs, bear_tfs, direction):
    n=len(bull_tfs) if direction=="buy" else len(bear_tfs)
    return {3:1.0,2:0.7,1:0.35}.get(n,0.0)

def score_candlestick(indicators, direction):
    cp=indicators.get("candlestick",{})
    if not cp.get("valid"): return 0.5
    strongest=cp.get("strongest")
    if not strongest: return 0.5
    strength=strongest.get("strength",1); t=strongest.get("type","neutral")
    if (direction=="buy" and t=="bullish") or (direction=="sell" and t=="bearish"):
        return min(1.0, 0.5+strength*0.15)
    elif t=="neutral": return 0.5
    else: return 0.3

def score_sr(indicators, direction):
    sr=indicators.get("support_resistance",{})
    if not sr.get("valid"): return 0.5
    key="distance_to_sup" if direction=="buy" else "distance_to_res"
    dist=sr.get(key)
    if dist is None: return 0.5
    if dist<=0.5: return 0.95
    elif dist<=1.5: return 0.80
    elif dist<=3.0: return 0.60
    else: return 0.40

def calc_composite_score(indicators, direction, bull_tfs=None, bear_tfs=None):
    bull_tfs=bull_tfs or []; bear_tfs=bear_tfs or []
    scores={
        "ema_alignment":  score_ema(indicators,direction),
        "rsi_zone":       score_rsi(indicators,direction),
        "macd_momentum":  score_macd(indicators,direction),
        "adx_strength":   score_adx(indicators),
        "bb_position":    score_bb(indicators,direction),
        "volume_confirm": score_volume(indicators),
        "mtf_resonance":  score_mtf(bull_tfs,bear_tfs,direction),
        "candlestick":    score_candlestick(indicators,direction),
        "sr_proximity":   score_sr(indicators,direction),
    }
    total_w=sum(v["weight"] for v in SCORE_WEIGHTS.values())
    weighted=sum(scores[k]*SCORE_WEIGHTS[k]["weight"] for k in scores)
    composite=round(weighted/total_w*100,1)
    breakdown={k:{"score":round(scores[k]*100,1),"weight":SCORE_WEIGHTS[k]["weight"],
                  "desc":SCORE_WEIGHTS[k]["desc"],"contribution":round(scores[k]*SCORE_WEIGHTS[k]["weight"]/total_w*100,1)} for k in scores}
    return {"composite":composite,"breakdown":breakdown,"raw_scores":scores,
            "grade":"A" if composite>=80 else "B" if composite>=65 else "C" if composite>=50 else "D"}

def kelly_position_size(win_rate, avg_rr, balance=None, half_kelly=True):
    balance=balance or ACCOUNT_BALANCE_TWD
    if win_rate<=0 or avg_rr<=0:
        return {"kelly_pct":2.0,"risk_twd":balance*0.02,"method":"default"}
    loss_rate=1-win_rate; kelly_f=(win_rate*avg_rr-loss_rate)/avg_rr
    if kelly_f<=0:
        return {"kelly_pct":0.0,"risk_twd":0,"method":"kelly_negative","warning":"策略期望值為負，建議暫停"}
    if half_kelly: kelly_f*=0.5
    kelly_f=min(kelly_f,0.05); kelly_f=max(kelly_f,0.005)
    return {"kelly_pct":round(kelly_f*100,2),"risk_twd":round(balance*kelly_f,0),
            "method":"half_kelly" if half_kelly else "full_kelly",
            "kelly_raw":round((win_rate*avg_rr-loss_rate)/avg_rr*100,2),"edge":round(win_rate*avg_rr-loss_rate,4)}

def calc_performance_metrics(equity_curve, trades=None):
    if not equity_curve or len(equity_curve)<2: return {"valid":False}
    peak=equity_curve[0]; max_dd=0.0
    for v in equity_curve:
        if v>peak: peak=v
        dd=(peak-v)/peak
        if dd>max_dd: max_dd=dd
    returns=[(equity_curve[i]-equity_curve[i-1])/equity_curve[i-1] for i in range(1,len(equity_curve))]
    if len(returns)>1:
        rf=0.05/252; excess=[r-rf for r in returns]; mean_e=sum(excess)/len(excess)
        std_e=math.sqrt(sum((r-mean_e)**2 for r in excess)/max(len(excess)-1,1))
        sharpe=round(mean_e/std_e*math.sqrt(252),3) if std_e>1e-10 and math.isfinite(mean_e/std_e) else 0
    else: sharpe=0
    total_ret=(equity_curve[-1]-equity_curve[0])/equity_curve[0]
    n_days=max(len(equity_curve),1)
    annual_ret=round((1+total_ret)**(252/n_days)-1,4) if n_days>0 else 0
    calmar=round(annual_ret/max_dd,3) if max_dd>0 and math.isfinite(annual_ret/max_dd) else 0
    wins=[t for t in (trades or []) if t.get("pnl_twd",t.get("pnl",0))>0]
    losses=[t for t in (trades or []) if t.get("pnl_twd",t.get("pnl",0))<0]
    max_consec=0; cur=0
    for t in (trades or []):
        if t.get("pnl_twd",t.get("pnl",0))<0: cur+=1; max_consec=max(max_consec,cur)
        else: cur=0
    avg_win =sum(t.get("pnl_twd",t.get("pnl",0)) for t in wins) /max(len(wins),1)
    avg_loss=sum(abs(t.get("pnl_twd",t.get("pnl",0))) for t in losses)/max(len(losses),1)
    wr=len(wins)/max(len(wins)+len(losses),1)
    return {"valid":True,"sharpe":sharpe,"sharpe_grade":"優" if sharpe>=2 else "良" if sharpe>=1.5 else "普" if sharpe>=1 else "差",
            "max_drawdown":round(max_dd*100,2),"dd_grade":"優" if max_dd<0.05 else "良" if max_dd<0.10 else "普" if max_dd<0.20 else "危險",
            "calmar":calmar,"annual_return":round(annual_ret*100,2),"total_return":round(total_ret*100,2),
            "win_rate":round(wr*100,1),"actual_rr":round(avg_win/avg_loss,2) if avg_loss>0 else 0,
            "max_consec_loss":max_consec,"n_trades":len(trades or []),"equity_start":equity_curve[0],"equity_end":equity_curve[-1]}

def detect_regime(market_overview, indicators=None):
    twii_chg=float(market_overview.get("index",{}).get("twii",{}).get("chg",0) or 0)
    vix=float(market_overview.get("index",{}).get("vix",{}).get("price",20) or 20)
    adx=float((indicators or {}).get("adx_value",0) or 0)
    if vix>=40:
        return {"regime":"crisis","regime_zh":"極端危機","action":"stop_all","strategy":"停止所有交易","allowed_directions":[],"size_multiplier":0}
    if vix>=30 or twii_chg<=-3.5:
        return {"regime":"high_vol","regime_zh":"高波動/大盤重挫","action":"reduce_size","strategy":"縮倉50%","allowed_directions":["buy"],"size_multiplier":0.5}
    if adx>=25 and twii_chg>0.5:
        return {"regime":"trending_bull","regime_zh":"強多趨勢","action":"trend_follow","strategy":"順勢做多","allowed_directions":["buy"],"size_multiplier":1.0}
    if adx>=25 and twii_chg<-0.5:
        return {"regime":"trending_bear","regime_zh":"強空趨勢","action":"trend_follow","strategy":"謹慎觀望","allowed_directions":["sell"],"size_multiplier":0.6}
    if adx<20 and abs(twii_chg)<0.5:
        return {"regime":"ranging","regime_zh":"震盪整理","action":"mean_revert","strategy":"縮小倉位","allowed_directions":["buy","sell"],"size_multiplier":0.7}
    return {"regime":"normal","regime_zh":"正常市場","action":"normal","strategy":"正常策略","allowed_directions":["buy","sell"],"size_multiplier":1.0}
