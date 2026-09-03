"""
signal_engine.py — 台股波段訊號引擎 v1.0
波段策略：週線確認趨勢 + 日線找入場 + 三段止盈
"""
import logging, math
from datetime import datetime, timezone
from typing import Optional, Dict
from config import (
    SIGNAL_THRESHOLDS as THRESH, SWING_PARAMS,
    CIRCUIT_BREAKER as CB, ACCOUNT_BALANCE_TWD,
    COMMISSION_RATE, TAX_RATE_SELL, SHARES_PER_LOT, MIN_COMMISSION,
)

logger = logging.getLogger(__name__)

def calc_trade_cost(price, shares, direction):
    total_value = price * shares
    commission  = max(MIN_COMMISSION, total_value * COMMISSION_RATE)
    tax         = total_value * TAX_RATE_SELL if direction == "sell" else 0
    total_cost  = commission + tax
    return {"total_value": round(total_value,0), "commission": round(commission,0),
            "tax": round(tax,0), "total_cost": round(total_cost,0),
            "cost_pct": round(total_cost/total_value*100,3) if total_value else 0}

def calc_position_size(price, stop_loss, balance=None, risk_pct=None, size_cat="中型股"):
    # ★ 修正：2026-08-30（第一輪）——投資人回測報告核對數字時發現：舊版「lots = max(1, ...)」不管
    # 風險預算(2%)或單一部位資金上限(30%)換算出來是多少，最少一定強迫買1張。股價較高、或停損距離
    # 較寬的標的（例如當時的 6669.TW），换算下來连0.1張都不到，也照樣被塞進1張，等於這筆交易的實際
    # 風險遠遠超出系統自己設定的2%風控上限。第一輪先把「無條件下限1張」改成「不足1張就直接不進場」，
    # 但這樣又衍生出新問題：台股以「張」(1000股)為最小交易單位的話，像台積電、聯發科、大立光這種
    # 高價股，只要停損距離夠寬，連1張的風險都會超過2%——不是策略選擇不進場，是「張」這個交易單位
    # 本身把這些標的鎖在門外，53檔 TW50+ETF 的回測裡有20檔完全交易不到就是這樣來的。
    # ★ 修正：2026-08-30（第二輪，本次）——改用台股零股(odd-lot)交易的「股」為單位，不再綁定
    # 1000股=1張的整張門檻。風險預算/單一部位上限換算出來多少股，就用多少股（無條件捨去到整股），
    # 換算後不足1股才不進場——這樣「用多少張」不再是進不進得了場的門檻，只有風險預算本身才是。
    # max_shares_map 沿用原本 max_lots_map 的分類上限精神（原本20/10/5/30張），改成等值的股數上限
    # （×1000），確保這層「大型股最多20張」的曝險保護仍然存在，只是允許中間值不必卡在整張邊界。
    # 注意：零股交易本身仍有實務限制（撮合時段/流動性跟整張不同，這裡沒有另外模擬），詳見文件說明。
    balance  = balance  or ACCOUNT_BALANCE_TWD
    risk_pct = risk_pct or 2.0
    max_risk = balance * risk_pct / 100
    sl_dist  = abs(price - stop_loss)
    if sl_dist <= 0 or price <= 0:
        return {"shares":0,"risk_twd":0,"risk_pct":0,"position_value":0,"margin_pct":0,"roundtrip_cost":0,"breakeven_pct":0}
    risk_per_share = sl_dist
    raw_shares = max_risk / risk_per_share
    max_shares_by_cap = (balance * 0.30) / price
    raw_shares = min(raw_shares, max_shares_by_cap)
    max_shares_map = {"大型股":20000,"中型股":10000,"小型股":5000,"ETF":30000}
    shares = min(max_shares_map.get(size_cat,10000), math.floor(raw_shares))
    if shares < 1:
        return {"shares":0,"risk_twd":0,"risk_pct":0,"position_value":0,"margin_pct":0,"roundtrip_cost":0,"breakeven_pct":0}
    position_value = shares * price
    buy_cost  = calc_trade_cost(price, shares, "buy")
    sell_cost = calc_trade_cost(price, shares, "sell")
    roundtrip = buy_cost["total_cost"] + sell_cost["total_cost"]
    return {
        "shares":         shares,
        "lots":           round(shares / SHARES_PER_LOT, 3),
        "risk_twd":       round(shares * risk_per_share, 0),
        "risk_pct":       round(shares * risk_per_share / balance * 100, 2),
        "position_value": round(position_value, 0),
        "margin_pct":     round(position_value / balance * 100, 1),
        "roundtrip_cost": round(roundtrip, 0),
        "breakeven_pct":  round(roundtrip / position_value * 100, 3) if position_value else 0,
    }

def calc_stop_loss_tw(direction, price, atr, indicators, size_cat="中型股", low_5d=None, high_5d=None):
    params  = SWING_PARAMS.get(size_cat, SWING_PARAMS["中型股"])
    sl_dist = atr * params["sl_atr_mult"]
    sl_dist = max(sl_dist, price * 0.015)
    sl_dist = min(sl_dist, price * 0.08)
    if direction == "buy":
        sl = price - sl_dist
        if low_5d: sl = min(sl, low_5d * 0.995)
        sr = indicators.get("support_resistance", {})
        sup = sr.get("nearest_support")
        if sup and sl < sup < price: sl = sup * 0.995
    else:
        sl = price + sl_dist
        if high_5d: sl = max(sl, high_5d * 1.005)
        sr = indicators.get("support_resistance", {})
        res = sr.get("nearest_resistance")
        if res and price < res < sl: sl = res * 1.005
    return round(sl, 2)

def calc_take_profits_tw(direction, price, stop_loss, size_cat="中型股"):
    params = SWING_PARAMS.get(size_cat, SWING_PARAMS["中型股"])
    risk   = abs(price - stop_loss)
    if direction == "buy":
        tp1 = price + risk * params["tp1_rr"]
        tp2 = price + risk * params["tp2_rr"]
        tp3 = price + risk * params["tp3_rr"]
    else:
        tp1 = price - risk * params["tp1_rr"]
        tp2 = price - risk * params["tp2_rr"]
        tp3 = price - risk * params["tp3_rr"]
    return {"tp1":round(tp1,2),"tp2":round(tp2,2),"tp3":round(tp3,2),
            "rr1":params["tp1_rr"],"rr2":params["tp2_rr"],"rr3":params["tp3_rr"],
            "risk":round(risk,2),"risk_pct":round(risk/price*100,2),"exit_plan":"各1/3分批出場"}

def check_multi_timeframe_tw(tf_data):
    from indicators import calc_all_indicators
    results = {}
    for tf_key in ["weekly","daily","hourly"]:
        d = tf_data.get(tf_key)
        if d:
            ind = calc_all_indicators(d)
            if ind.get("valid"): results[tf_key] = ind
    if "daily" not in results:
        return {"direction":"none","score":0,"resonance":False,"conditions_met":[],"conditions_fail":[],
                "weekly_bias":"unknown","daily_bias":"unknown","rsi_value":50,"adx_value":0,"vol_ratio":1.0,"entry_indicators":{}}
    daily    = results["daily"]
    ema_d    = daily.get("ema",{})
    rsi_d    = daily.get("rsi",{})
    macd_d   = daily.get("macd",{})
    adx_d    = daily.get("adx",{})
    vol_d    = daily.get("volume",{})
    ema_bias = ema_d.get("bias","neutral")  if ema_d.get("valid")  else "neutral"
    rsi_val  = rsi_d.get("value",50)        if rsi_d.get("valid")  else 50
    macd_bias= macd_d.get("bias","neutral") if macd_d.get("valid") else "neutral"
    adx_val  = adx_d.get("value",0)         if adx_d.get("valid")  else 0
    vol_ratio= vol_d.get("ratio",1.0)       if vol_d.get("valid")  else 1.0
    weekly_bias = "neutral"
    if "weekly" in results:
        wk_ema = results["weekly"].get("ema",{})
        weekly_bias = wk_ema.get("bias","neutral") if wk_ema.get("valid") else "neutral"
    bull_score=0; bear_score=0; conds_met=[]; conds_fail=[]
    if "bullish" in ema_bias:   bull_score+=3; conds_met.append(f"EMA {ema_d.get('alignment','')} ✓")
    elif "bearish" in ema_bias: bear_score+=3; conds_met.append(f"EMA {ema_d.get('alignment','')} ✓")
    else: conds_fail.append("EMA 方向不明")
    price = daily.get("current_price",0); e120 = ema_d.get("e120") or ema_d.get("e_trend",0)
    if price > 0 and e120 > 0:
        if price > e120 * 1.005:   bull_score+=2; conds_met.append(f"突破半年線({e120:.1f}) ✓")
        elif price < e120 * 0.995: bear_score+=2; conds_met.append(f"跌破半年線({e120:.1f}) ✓")
        else: conds_fail.append(f"在半年線附近({e120:.1f})")
    if 45<=rsi_val<=70:    bull_score+=1; conds_met.append(f"RSI {rsi_val:.0f} 多頭健康區 ✓")
    elif 30<=rsi_val<45:   bear_score+=1; conds_met.append(f"RSI {rsi_val:.0f} 空頭區 ✓")
    elif rsi_val>75:       conds_fail.append(f"RSI {rsi_val:.0f} 過熱")
    elif rsi_val<30:       bull_score+=2; conds_met.append(f"RSI {rsi_val:.0f} 超賣反彈 ✓")
    if "bullish" in macd_bias:
        bull_score+=1; cross=macd_d.get("cross","")
        if cross=="MACD金叉": bull_score+=1; conds_met.append("MACD 金叉 ✓")
        else: conds_met.append("MACD 偏多 ✓")
    elif "bearish" in macd_bias: bear_score+=1; conds_met.append("MACD 偏空 ✓")
    else: conds_fail.append("MACD 中性")
    if vol_ratio>=THRESH["min_vol_ratio"]:  bull_score+=2; conds_met.append(f"量增({vol_ratio:.1f}x) ✓")
    elif vol_ratio<0.7: conds_fail.append(f"量縮({vol_ratio:.1f}x)")
    if adx_val>=25:
        if bull_score>bear_score: bull_score+=1
        elif bear_score>bull_score: bear_score+=1
        conds_met.append(f"ADX {adx_val:.0f} 趨勢強 ✓")
    elif adx_val<THRESH["min_adx"]: conds_fail.append(f"ADX {adx_val:.0f} 趨勢不足")
    if weekly_bias!="neutral":
        if bull_score>bear_score and "bearish" in weekly_bias:
            bull_score=max(0,bull_score-2); conds_fail.append("⚠️ 週線偏空，逆勢風險")
        elif bear_score>bull_score and "bullish" in weekly_bias:
            bear_score=max(0,bear_score-2); conds_fail.append("⚠️ 週線偏多，逆勢風險")
        else: conds_met.append(f"週線確認({weekly_bias}) ✓")
    direction = "buy" if bull_score>=5 and bull_score>bear_score else "sell" if bear_score>=5 and bear_score>bull_score else "none"
    if direction=="none": score=0
    else:
        total=bull_score+bear_score; active=bull_score if direction=="buy" else bear_score
        base=(active/max(total,1))*60+30
        resonance="hourly" in results and (("bullish" in results["hourly"].get("ema",{}).get("bias","") and direction=="buy") or ("bearish" in results["hourly"].get("ema",{}).get("bias","") and direction=="sell"))
        if resonance: base+=10
        score=min(100,int(base))
    return {"direction":direction,"score":score,"resonance":"hourly" in results,
            "bull_score":bull_score,"bear_score":bear_score,"weekly_bias":weekly_bias,"daily_bias":ema_bias,
            "rsi_value":rsi_val,"adx_value":adx_val,"vol_ratio":vol_ratio,
            "conditions_met":conds_met,"conditions_fail":conds_fail,"entry_indicators":daily}

def generate_signal_tw(ticker, stock_info, tf_data, market_overview, inst_data=None, margin_data=None):
    try:
        from indicators import calc_all_indicators
        name=stock_info.get("name",ticker); sector=stock_info.get("sector","其他")
        size_cat=stock_info.get("size_cat","中型股"); code=stock_info.get("code",ticker.replace(".TW","").replace(".TWO",""))
        if not market_overview.get("can_trade",True): return None
        daily_data=tf_data.get("daily")
        if not daily_data: return None
        daily_ind=calc_all_indicators(daily_data)
        if not daily_ind.get("valid"): return None
        price=daily_data.get("current_price",0)
        if price<=0: return None
        closes=daily_data.get("closes",[]); highs=daily_data.get("highs",[]); lows=daily_data.get("lows",[])
        low_5d=min(lows[-5:]) if len(lows)>=5 else None; high_5d=max(highs[-5:]) if len(highs)>=5 else None
        mtf=check_multi_timeframe_tw(tf_data)
        direction=mtf.get("direction","none"); score=mtf.get("score",0)
        # ★ 診斷用：2026-09-03——今天兩次全市場掃描（間隔52分鐘、大盤/三大法人
        # 彙總數字完全相同）掃到的候選訊號數量差異巨大（123→2），且入選的個股
        # 完全不同，目前懷疑是資料源（Yahoo/yfinance）在收盤後到資料完全定案
        # 之間，個股最後一根日K有被事後修正/補齊的情況，但沒有直接證據。這裡
        # 只在 direction!=none（代表已經接近或達到訊號門檻）時記一行診斷 log，
        # 包含最後一根K棒的日期/收盤價，下次比對兩次掃描的原始分數/資料時間點
        # 就有直接證據，而不是只能用大盤等間接數字推測。確認根因後應移除。
        if direction != "none":
            _last_date = (daily_data.get("dates") or [None])[-1]
            logger.info(f"[SCORE] {ticker} dir={direction} score={score} "
                        f"bar={_last_date} close={price}")
        if direction=="none" or score<THRESH["min_score"]: return None
        adx_val=mtf.get("adx_value",0)
        if adx_val<THRESH["min_adx"]: return None
        vol_ratio=mtf.get("vol_ratio",1.0)
        if vol_ratio<THRESH["min_vol_ratio"] and score<75: return None
        inst_signal=""; inst_score_bonus=0
        if inst_data:
            fn=inst_data.get("foreign_net",0); tn=inst_data.get("trust_net",0)
            if direction=="buy":
                if fn>200: inst_score_bonus+=5; inst_signal=f"外資買超 {fn:+,}張"
                elif fn<-200: inst_score_bonus-=5; inst_signal=f"外資賣超 {fn:+,}張 ⚠️"
            else:
                if fn<-200: inst_score_bonus+=5; inst_signal=f"外資賣超 {fn:+,}張"
        score=min(100,score+inst_score_bonus)
        if score<THRESH["min_score"]: return None
        atr_info=daily_ind.get("atr",{}); atr=atr_info.get("value",price*0.02) or price*0.02
        sl=calc_stop_loss_tw(direction,price,atr,daily_ind,size_cat,low_5d,high_5d)
        tp_info=calc_take_profits_tw(direction,price,sl,size_cat)
        if tp_info["rr1"]<THRESH["min_rr"]: return None
        pos=calc_position_size(price,sl,size_cat=size_cat)
        if pos["shares"]<=0: return None
        ema_ind=daily_ind.get("ema",{})
        ema5_val=ema_ind.get("e_fast") if ema_ind.get("valid") else None
        entry_zone_low=round(min(price*0.98, ema5_val*0.99) if ema5_val else price*0.98, 2)
        entry_zone_high=round(price*1.005,2)
        sig_id=f"{ticker}_{direction}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        grade="A" if score>=85 else "B" if score>=75 else "C"
        action={"A":"🔥 強力訊號，建議進場","B":"✅ 良好訊號，可以考慮","C":"👀 待觀察，可小量試探"}[grade]
        emoji_g={"A":"🟢","B":"🟡","C":"⚪"}[grade]
        vol_desc=f"爆量({vol_ratio:.1f}x)" if vol_ratio>=2.0 else f"量增({vol_ratio:.1f}x)" if vol_ratio>=1.5 else f"正常量({vol_ratio:.1f}x)"
        weekly_desc={"bullish":"週線多頭","strong_bullish":"週線強多","bearish":"週線空頭","neutral":"週線橫盤"}.get(mtf.get("weekly_bias","neutral"),"週線中性")
        reason_full=(f"【技術面】{ema_ind.get('alignment','')} / RSI {mtf['rsi_value']:.0f} / "
                     f"MACD {'多' if 'bullish' in daily_ind.get('macd',{}).get('bias','') else '空'}頭動能\n"
                     f"【量能】{vol_desc}\n【趨勢】{weekly_desc} / ADX {adx_val:.0f}\n"
                     f"【法人】{inst_signal or '資料更新中'}\n"
                     f"【風控】止損 {round(abs(price-sl)/price*100,1)}%，TP1 盈虧比 1:{tp_info['rr1']}")
        reason_brief=(f"{ema_ind.get('alignment','')} + {vol_desc}\n"
                      f"止損 {round(abs(price-sl)/price*100,1)}% / TP1 +{round(abs(tp_info['tp1']-price)/price*100,1)}%")
        signal={
            "id":sig_id,"ticker":ticker,"code":code,"name":name,"sector":sector,"size_cat":size_cat,
            "emoji":stock_info.get("emoji","📊"),"emoji_grade":emoji_g,
            "direction":direction,"direction_zh":"做多 📈" if direction=="buy" else "做空 📉",
            "score":score,"grade":grade,"action":action,
            "current_price":price,"change_pct":daily_data.get("change_pct",0),
            "entry_price":price,"entry_zone_low":entry_zone_low,"entry_zone_high":entry_zone_high,
            "stop_loss":sl,"sl_pct":round(abs(price-sl)/price*100,2),
            "tp1":tp_info["tp1"],"tp2":tp_info["tp2"],"tp3":tp_info["tp3"],
            "tp1_pct":round(abs(tp_info["tp1"]-price)/price*100,2),
            "tp2_pct":round(abs(tp_info["tp2"]-price)/price*100,2),
            "tp3_pct":round(abs(tp_info["tp3"]-price)/price*100,2),
            "rr1":tp_info["rr1"],"rr2":tp_info["rr2"],"rr3":tp_info["rr3"],"exit_plan":tp_info["exit_plan"],
            "suggested_shares":pos["shares"],"suggested_lots":pos["lots"],"risk_twd":pos["risk_twd"],"risk_pct":pos["risk_pct"],
            "position_value":pos["position_value"],"roundtrip_cost":pos["roundtrip_cost"],"breakeven_pct":pos["breakeven_pct"],
            "adx_value":adx_val,"rsi_value":mtf["rsi_value"],"vol_ratio":vol_ratio,"atr":round(atr,2),
            "inst_signal":inst_signal,"weekly_bias":mtf["weekly_bias"],
            "reason_brief":reason_brief,"reason_full":reason_full,
            "conditions_met":mtf["conditions_met"],"conditions_fail":mtf["conditions_fail"],
            "generated_at":datetime.now(timezone.utc).isoformat(),"expire_days":CB["signal_expire_days"],
            "result":"pending","pnl_twd":0,"status":"active",
        }
        logger.info(f"[{ticker}] ✅ {name} {direction} score={score}({grade}) SL={sl:.1f} TP1={tp_info['tp1']:.1f} shares={pos['shares']}")
        return signal
    except Exception as e:
        logger.error(f"generate_signal_tw {ticker}: {e}", exc_info=True); return None
