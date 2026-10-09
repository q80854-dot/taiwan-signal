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
    MAX_RISK_PER_TRADE, ENABLE_SHORT_SIGNALS,
    SHORT_SIGNAL_THRESH, SHORT_MAX_SENTIMENT, SHORT_REQUIRE_WEEKLY_BEARISH,
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

def calc_position_size(price, stop_loss, balance=None, risk_pct=None, size_cat="中型股", avg_volume_lots=None):
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
    # ★ 修正：2026-09-16——稽核時發現 risk_pct 預設值是寫死的字面常數 2.0，跟
    # config.py 的 MAX_RISK_PER_TRADE=0.02 只是「數字剛好相同」，兩者並沒有真的
    # 連在一起——以後如果改 config.py 的風險上限，這裡會完全沒反應，形成一個
    # 「看起來可調、實際上調了沒用」的陷阱。改成直接從 config 讀，兩處數字
    # 保證永遠一致。
    balance  = balance  or ACCOUNT_BALANCE_TWD
    risk_pct = risk_pct or (MAX_RISK_PER_TRADE * 100)
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
    # ★ 新增：2026-09-16——ChatGPT在三方交叉比對（Perplexity/ChatGPT/Gemini）中
    # 提出、經評估後採納的建議：部位大小過去只看帳戶風險預算(2%)換算出來的股數，
    # 完全沒檢查這個股數是不是已經佔該股票當日成交量一個不合理的比例——小型股
    # 如果建議部位過大，使用者實際下單可能自己把價格打高/打低，形成自己造成的
    # 滑價，讓風報比在下單當下就被侵蝕。這裡用 stock_universe.py 既有的
    # volume_lots（該股票最近一次品種清單更新時的當日成交張數，是流動性的粗略
    # 代理值，不是嚴格的多日移動平均，之後如果要更精確可以改成真正的多日均量）
    # 把建議部位上限訂在「不超過當日成交量的10%」，這是常見的保守零售/量化
    # 交易實務準則，用來避免自己的委託單造成過大的價格衝擊。只在呼叫端有提供
    # avg_volume_lots 時才套用，沒提供時維持原本行為，不會因為缺這筆資料就
    # 完全算不出部位。
    liquidity_capped = False
    if avg_volume_lots:
        liquidity_cap_shares = math.floor(avg_volume_lots * SHARES_PER_LOT * 0.10)
        if liquidity_cap_shares < shares:
            shares = liquidity_cap_shares
            liquidity_capped = True
    if shares < 1:
        return {"shares":0,"risk_twd":0,"risk_pct":0,"position_value":0,"margin_pct":0,"roundtrip_cost":0,
                "breakeven_pct":0,"liquidity_capped":liquidity_capped}
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
        "liquidity_capped": liquidity_capped,
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
    # ★ 修正：2026-10-05——使用者回報「停損次數太高、大部分都是停損」。實查 9/30
    # 艾笛森：ATR=1.4、現價27.45，依設定中型股應用 2.0×ATR≈2.8 元的停損，但上面
    # 「最近支撐位在 ATR 停損與現價之間就把停損改成支撐下方」這一行把停損從 24.65
    # 收緊成 26.71（只剩 0.53 ATR、2.7%）——日內正常波動（這檔 ATR 佔股價 5%）就足以
    # 掃到，隔天 10/1 果然被洗出場。註解當初寫的是避免卡在雜訊區，實際效果卻是
    # 相反（往內收）。這裡補上硬下限：不論結構支撐/壓力多近，停損距離至少
    # 1.5×ATR，才不會小於一天正常震盪幅度。支撐離得更遠時維持原本較寬的停損。
    min_dist = max(atr * 1.5, price * 0.015)
    if direction == "buy" and (price - sl) < min_dist:
        sl = price - min_dist
    elif direction != "buy" and (sl - price) < min_dist:
        sl = price + min_dist
    # ★ 修正：2026-10-09——最前面已把停損夾在「最多 8%」，但上面的結構支撐（low_5d／high_5d）
    # 與 1.5×ATR 下限可能把停損再放寬，對高波動股（ATR>股價 5.3%）會「突破 8% 上限」，
    # 讓單筆風險比設定值大。這裡在所有調整後再套一次 1.5%～8% 的硬界限，確保 8% 風險上限
    # 真的成立（極高波動股因此會被夾在 8%，由部位大小換算自動縮小張數，維持 2% 帳戶風險）。
    dist = min(max(abs(price - sl), price * 0.015), price * 0.08)
    sl = price - dist if direction == "buy" else price + dist
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

# ★ 新增：2026-09-26——稽核報告中長期項目「因子/評分消融分析」。原本
# check_multi_timeframe_tw() 的六個評分因子（EMA排列/半年線突破/RSI/MACD/
# 量增/ADX加成/週線逆勢懲罰）全部寫死在同一段流程裡，沒有任何方式可以
# 「單獨關掉某一項，看勝率/報酬有沒有掉」——想知道哪個因子真的有貢獻、
# 哪個只是雜訊甚至扣分，只能改程式碼重新部署才能測，成本太高、也太危險
# （改程式碼直接影響實盤評分）。這裡加一個 disabled_factors 參數（預設
# None，等同完全不影響任何行為，實盤呼叫端不用改），backtester.py 的
# run_factor_ablation_tw() 會逐一把每個因子丟進這裡、重跑整批回測，藉此
# 量化每個因子對勝率/報酬的邊際貢獻，而不需要動到任何實盤程式碼路徑。
FACTOR_KEYS = ("ema", "trend200", "rsi", "macd", "volume", "adx", "weekly")

def check_multi_timeframe_tw(tf_data, disabled_factors=None):
    from indicators import calc_all_indicators
    disabled = disabled_factors or set()
    results = {}
    for tf_key in ["weekly","daily","hourly"]:
        d = tf_data.get(tf_key)
        if d:
            ind = calc_all_indicators(d)
            if ind.get("valid"): results[tf_key] = ind
    if "daily" not in results:
        return {"direction":"none","score":0,"resonance":False,"conditions_met":[],"conditions_fail":[],
                "weekly_bias":"unknown","daily_bias":"unknown","rsi_value":50,"adx_value":0,"adx_bias":"neutral",
                "vol_ratio":1.0,"entry_indicators":{}}
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
    # ★ 新增：2026-09-28——ChatGPT/Perplexity 審查放空門檻時都指出同一個問題：
    # ADX 只衡量「趨勢強度」，不衡量「趨勢方向」——ADX=25 可能是強漲也可能是
    # 強跌，放空訊號如果只拿 ADX 數值當門檻（見 config.py SHORT_SIGNAL_THRESH
    # min_adx），很可能誤放行「ADX很高但其實是強漲」的股票。calc_adx() 其實
    # 早就算出 +DI/-DI（bias 欄位：+DI>-DI 是 bullish、反之 bearish），只是
    # 原本沒有任何地方讀取，這裡把它帶出去，讓 generate_signal_tw() 的放空
    # 判斷可以額外要求 -DI>+DI（真正的空方動能主導），不是只看 ADX 強度。
    adx_bias = adx_d.get("bias","neutral")  if adx_d.get("valid")  else "neutral"
    vol_ratio= vol_d.get("ratio",1.0)       if vol_d.get("valid")  else 1.0
    weekly_bias = "neutral"
    if "weekly" in results:
        wk_ema = results["weekly"].get("ema",{})
        weekly_bias = wk_ema.get("bias","neutral") if wk_ema.get("valid") else "neutral"
    bull_score=0; bear_score=0; conds_met=[]; conds_fail=[]
    if "ema" not in disabled:
        if "bullish" in ema_bias:   bull_score+=3; conds_met.append(f"EMA {ema_d.get('alignment','')} ✓")
        elif "bearish" in ema_bias: bear_score+=3; conds_met.append(f"EMA {ema_d.get('alignment','')} ✓")
        else: conds_fail.append("EMA 方向不明")
    price = daily.get("current_price",0); e120 = ema_d.get("e120") or ema_d.get("e_trend",0)
    if "trend200" not in disabled and price > 0 and e120 > 0:
        if price > e120 * 1.005:   bull_score+=2; conds_met.append(f"突破半年線({e120:.1f}) ✓")
        elif price < e120 * 0.995: bear_score+=2; conds_met.append(f"跌破半年線({e120:.1f}) ✓")
        else: conds_fail.append(f"在半年線附近({e120:.1f})")
    # ★ 修正：2026-09-16——稽核發現這幾個區塊原本只有「多頭方向」有加分規則，
    # 空頭方向完全沒有對稱的加分，導致 sell 訊號的「本方理論最高分」實際上比
    # buy 訊號低很多（連鎖影響到上面新公式假設的 MAX_ACTIVE_SCORE=12 只有 buy
    # 摸得到，sell 頂多到 8 分，永遠評不到 A 級）。這裡補上對稱規則：
    # RSI 超買（>75）比照超賣反彈(+2) 給空頭「超買反轉」+2；
    # MACD 死叉比照金叉，給空頭 +1 額外加成。
    if "rsi" not in disabled:
        if 45<=rsi_val<=70:    bull_score+=1; conds_met.append(f"RSI {rsi_val:.0f} 多頭健康區 ✓")
        elif 30<=rsi_val<45:   bear_score+=1; conds_met.append(f"RSI {rsi_val:.0f} 空頭區 ✓")
        elif rsi_val>75:       bear_score+=2; conds_met.append(f"RSI {rsi_val:.0f} 超買反轉 ✓")
        elif rsi_val<30:       bull_score+=2; conds_met.append(f"RSI {rsi_val:.0f} 超賣反彈 ✓")
    if "macd" not in disabled:
        if "bullish" in macd_bias:
            bull_score+=1; cross=macd_d.get("cross","")
            if cross=="MACD金叉": bull_score+=1; conds_met.append("MACD 金叉 ✓")
            else: conds_met.append("MACD 偏多 ✓")
        elif "bearish" in macd_bias:
            bear_score+=1; cross=macd_d.get("cross","")
            if cross=="MACD死叉": bear_score+=1; conds_met.append("MACD 死叉 ✓")
            else: conds_met.append("MACD 偏空 ✓")
        else: conds_fail.append("MACD 中性")
    # ★ 修正：2026-09-16——稽核發現量增(vol_ratio)原本無條件只加到 bull_score，
    # 即使當天所有其他指標都偏空、只有成交量放大，也會被硬塞進多頭分數，等於
    # 「爆量下跌」這種明顯偏空的量價訊號反而幫多頭加分。改成比照下面 ADX
    # 加成的寫法，加到「目前領先的一方」（跟 EMA/半年線/RSI/MACD 已經判斷出
    # 的方向一致），量增才會是「確認當前趨勢」而不是「無條件挺多」。
    if "volume" not in disabled:
        if vol_ratio>=THRESH["min_vol_ratio"]:
            if bull_score>=bear_score: bull_score+=2
            else: bear_score+=2
            conds_met.append(f"量增({vol_ratio:.1f}x) ✓")
        elif vol_ratio<0.7: conds_fail.append(f"量縮({vol_ratio:.1f}x)")
    if "adx" not in disabled:
        if adx_val>=25:
            if bull_score>bear_score: bull_score+=1
            elif bear_score>bull_score: bear_score+=1
            conds_met.append(f"ADX {adx_val:.0f} 趨勢強 ✓")
        elif adx_val<THRESH["min_adx"]: conds_fail.append(f"ADX {adx_val:.0f} 趨勢不足")
    if "weekly" not in disabled and weekly_bias!="neutral":
        if bull_score>bear_score and "bearish" in weekly_bias:
            bull_score=max(0,bull_score-2); conds_fail.append("⚠️ 週線偏空，逆勢風險")
        elif bear_score>bull_score and "bullish" in weekly_bias:
            bear_score=max(0,bear_score-2); conds_fail.append("⚠️ 週線偏多，逆勢風險")
        else: conds_met.append(f"週線確認({weekly_bias}) ✓")
    direction = "buy" if bull_score>=5 and bull_score>bear_score else "sell" if bear_score>=5 and bear_score>bull_score else "none"
    if direction=="none": score=0
    else:
        # ★ 修正：2026-09-16——使用者回報「交易訊號幾乎都錯誤」，追查後發現原本
        # base=(active/max(total,1))*60+30 是用「本方分數佔（本方+對方）分數的
        # 比例」換算信心分數。這個公式有嚴重瑕疵：只要對方（bear_score 或
        # bull_score，視方向而定）剛好是 0——這在成交量稀薄、盤整的ETF/債券型
        # ETF 身上非常常見，只是各項指標剛好落在中性區、不代表趨勢真的強——
        # active/total 就會等於 1.0，不論本方分數是剛好卡在最低門檻 5 分（勉強
        # 達標，缺乏強力佐證）還是滿分 12 分（真正多重確認），都會算出 base=90，
        # 再加小時線共振 +10 直接封頂 100 分、評級(A)「🔥 強力訊號，建議進場」。
        # 結果是大量勉強達標、證據薄弱的邊緣訊號被貼上跟真正多重確認訊號一樣的
        # 最高信心標籤（實測 2026-09-09 單次掃描：338 檔候選中絕大多數 ETF 都
        # 顯示 score=100，包含債券型ETF這種波動極小、理論上很少會多指標同時
        # 共振的標的），使用者完全無法從分數判斷訊號品質，這正是訊號品質差的
        # 根本原因。
        # 修正後直接用「本方分數 ÷ 該方向理論最高分」換算，分數只反映本方證據
        # 的絕對強度，不再受「對方剛好是 0」這種巧合影響；對方若真的有分數（有
        # 反向證據），已經透過 bull_score>bear_score 的方向判定、以及週線逆勢時
        # 直接扣減本方分數（上面第 155-158 行）反映在 active 裡，不需要再疊加一
        # 次比例懲罰。理論最高本方分數：EMA(3)+半年線(2)+RSI(最高2)+MACD含金叉
        # (2)+爆量(2)+ADX加成(1) = 12。修正後，剛好卡門檻的 bull_score=5 只會
        # 算出 55 分（低於 min_score=65，會被 generate_signal_tw() 正常過濾掉，
        # 不會再被推播），必須有更多指標真的同向確認才能達到 65 分以上。
        MAX_ACTIVE_SCORE = 12
        active = bull_score if direction=="buy" else bear_score
        base = 30 + (active/MAX_ACTIVE_SCORE)*60
        resonance="hourly" in results and (("bullish" in results["hourly"].get("ema",{}).get("bias","") and direction=="buy") or ("bearish" in results["hourly"].get("ema",{}).get("bias","") and direction=="sell"))
        if resonance: base+=10
        score=min(100,int(base))
    return {"direction":direction,"score":score,"resonance":"hourly" in results,
            "bull_score":bull_score,"bear_score":bear_score,"weekly_bias":weekly_bias,"daily_bias":ema_bias,
            "rsi_value":rsi_val,"adx_value":adx_val,"adx_bias":adx_bias,"vol_ratio":vol_ratio,
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
        # ★ 2026-10-05 影子追蹤（shadow.py）：
        #   · 沒有訊號的股票（direction=none 或分數不足）隨機抽 3% 當「對照組」；
        #   · 分數已達門檻的標的先記下特徵快照，之後若被下面任何一條規則擋下，會把「假想單」記錄起來
        #     追蹤結果，用來驗證那條規則到底有沒有幫到我們。
        # 只有掃描器呼叫 shadow.begin_run() 之後才會收集，回測／其他呼叫端完全不受影響。
        try:
            import shadow as _sh
        except Exception:
            _sh = None
        if direction=="none" or score<THRESH["min_score"]:
            if _sh is not None:
                _sh.control_from_engine(ticker, stock_info, daily_data, daily_ind, mtf, market_overview)
            return None
        if _sh is not None:
            _sh.observe(ticker, daily_data, daily_ind, mtf, market_overview)
        def _rej(reason, sl_=None):
            if _sh is not None:
                _sh.note_reject(ticker, stock_info, daily_data, daily_ind, mtf, direction, score, reason,
                                market_overview, sl_)
        # ★ 修正：2026-09-28（第二輪）——上一輪重新開放放空後，把 ChatGPT／
        # Perplexity 兩邊的審查意見貼回去複查，兩邊獨立收斂出同一個架構問題：
        # 原本把「大盤環境」「個股方向」「個股訊號強度」全部塞進同一組
        # if 判斷依序檢查，等於同一個資訊（例如週線偏空、ADX）可能同時影響
        # 「算分」跟「准不准放行」兩層，也讓「大盤今天心情不好」跟「這檔股票
        # 真的走空」這兩件完全不同的事混在一起判斷。這裡改成明確分層，
        # 上層 gate 沒過，下層完全不看，不讓個股分數把大盤層級的否決救回來：
        #   Layer 1  Market Regime Gate：大盤是不是處於「允許放空」的中期
        #            環境（fetch_market_regime()，20日/60日均線+斜率，多日
        #            趨勢，不是單日漲跌）。這一層通不過，直接 NO SHORT。
        #   Layer 2  Stock Direction Gate：這一檔個股本身的方向證據是否
        #            明確偏空——週線必須明確偏空（硬性，不是軟性扣分）、
        #            且 ADX 的 +DI/-DI 方向也要確認是空方主導（不能只看
        #            ADX 強度，ADX 高可能是強漲也可能是強跌，見下方 adx_bias）。
        #   Layer 3  訊號強度：分數/ADX強度/量能門檻本身比做多更高。
        if direction=="sell":
            if not ENABLE_SHORT_SIGNALS:
                _rej("short_off"); return None
            # --- Layer 1: Market Regime Gate -------------------------------
            regime = market_overview.get("regime", {})
            if not regime.get("available"):
                # 抓不到大盤中期趨勢資料 = 不確定現在是不是空頭環境，
                # 「不確定」不等於「確定可以放空」，保守起見直接不放行
                # （跟 fetch_market_regime() 的 fail-closed 說明一致）。
                logger.info(f"[{ticker}] 放空 Layer1 Market Regime Gate 未過：大盤中期趨勢資料不可用")
                _rej("gate_regime"); return None
            if not regime.get("bearish_regime"):
                logger.info(f"[{ticker}] 放空 Layer1 Market Regime Gate 未過：大盤中期趨勢={regime.get('trend')}"
                            f"（非明確中期空頭，不允許個股層級放空訊號蓋過大盤環境判斷）")
                _rej("gate_regime"); return None
            # 大盤單日急跌/急漲（shock）仍額外檢查一次：就算中期是空頭，
            # 如果今天大盤单日已經是極端反彈（情緒分數很高），也先觀望一天，
            # 避免對著當天的軋空行情放空；這是 Layer1 內的次要保護，不是
            # 主要開關（主要開關已經是上面的 regime，不會被單日數字反過來
            # 打開放空，只會在 regime 已經允許時，被單日異常反彈暫時攔一次）。
            if market_overview.get("sentiment_score", 50) >= SHORT_MAX_SENTIMENT:
                logger.info(f"[{ticker}] 放空 Layer1 Market Shock 檢查未過：大盤單日情緒分數過高，"
                            f"疑似當日軋空行情，暫緩本次放空訊號")
                _rej("gate_shock"); return None
            # --- Layer 2: Stock Direction Gate ------------------------------
            if SHORT_REQUIRE_WEEKLY_BEARISH and "bearish" not in mtf.get("weekly_bias","neutral"):
                _rej("gate_weekly"); return None
            adx_bias = mtf.get("adx_bias","neutral")
            if adx_bias != "bearish":
                # ADX 只衡量趨勢強度、不衡量方向，見 check_multi_timeframe_tw()
                # 的 adx_bias 說明：+DI>-DI 時 ADX 再高也是多方強勢，不能拿來
                # 當放空的訊號強度證據。
                logger.info(f"[{ticker}] 放空 Layer2 Stock Direction Gate 未過：ADX方向(+DI/-DI)={adx_bias}，"
                            f"非空方主導，即使ADX數值達標也不視為有效放空訊號")
                _rej("gate_adxdir"); return None
            # --- Layer 3: 訊號強度（分數/ADX強度/量能，比做多更嚴格）--------
            if score < SHORT_SIGNAL_THRESH["min_score"]:
                _rej("short_score"); return None
        adx_val=mtf.get("adx_value",0)
        # 放空的 ADX/量能門檻比做多更嚴格（見 config.py SHORT_SIGNAL_THRESH 說明）
        min_adx_req = SHORT_SIGNAL_THRESH["min_adx"] if direction=="sell" else THRESH["min_adx"]
        if adx_val<min_adx_req:
            _rej("adx_low"); return None
        vol_ratio=mtf.get("vol_ratio",1.0)
        min_vol_req = SHORT_SIGNAL_THRESH["min_vol_ratio"] if direction=="sell" else THRESH["min_vol_ratio"]
        if vol_ratio<min_vol_req and score<75:
            _rej("vol_low"); return None
        # ★ 修正：2026-09-16——稽核發現 sell 方向的法人加權只有「外資賣超 → +5」
        # 這一種情況，buy 方向卻同時有「外資買超 → +5」跟「外資賣超 → -5」兩種。
        # 也就是說，一檔 sell(放空)訊號就算外資當天大買超（跟「放空」方向完全
        # 相反的反向證據），分數也完全不會被扣，等於少了一半的法人風控保護，
        # 這正是使用者說「發現很多訊號錯誤」時應該一併稽核到的不對稱問題。
        # 這裡補上對稱的扣分規則：sell 訊號遇到外資買超（反向證據），比照 buy
        # 訊號遇到外資賣超一樣扣 5 分。
        inst_signal=""; inst_score_bonus=0
        if inst_data:
            fn=inst_data.get("foreign_net",0); tn=inst_data.get("trust_net",0)
            if direction=="buy":
                if fn>200: inst_score_bonus+=5; inst_signal=f"外資買超 {fn:+,}張"
                elif fn<-200: inst_score_bonus-=5; inst_signal=f"外資賣超 {fn:+,}張 ⚠️"
            else:
                if fn<-200: inst_score_bonus+=5; inst_signal=f"外資賣超 {fn:+,}張"
                elif fn>200: inst_score_bonus-=5; inst_signal=f"外資買超 {fn:+,}張 ⚠️"
        score=min(100,score+inst_score_bonus)
        # ★ 新增：2026-09-24——使用者要求強化現有總經指標的權重。稽核發現
        # risk_manager.run_all_checks() 雖然定義了完整的 score_adj 邏輯（大盤
        # 熔斷/外資賣超/融資急縮都會扣分），但整個專案裡從來沒有任何地方真正
        # 呼叫它——是另一個「設計了、卻沒接線」的機制（跟 margin_change_warning
        # 同一類問題）。這裡直接把大盤層級的總經檢查（跟持倉數量/帳戶餘額無關、
        # 每檔訊號都適用的那幾項）接進評分，讓總經風險真正反映在訊號分數上，
        # 而不是只停在 risk_manager.py 裡一段從未執行過的程式碼。
        try:
            from risk_manager import check_market_circuit_breaker, check_foreign_flow, check_margin_change
            macro_adj = 0; macro_notes = []
            mc = check_market_circuit_breaker(market_overview)
            fc = check_foreign_flow(market_overview)
            # ★ 修正：2026-09-29——使用者回報「市場環境頁」同一頁一邊寫「外資賣超
            # 632億，暫停多單」，一邊又寫「可交易：是」「產業✅適合交易」，互相
            # 矛盾。追出來的根因：check_market_circuit_breaker()／check_foreign_flow()
            # 在大盤重挫或外資極端賣超時，回傳的 level 是 "extreme"、
            # action 是 "stop_buy"——但這個 "extreme"／"stop_buy" 只有這裡（
            # signal_engine.py 算單一訊號分數）跟 risk_manager.get_system_status()
            # （市場環境頁的顯示邏輯）兩個地方各自獨立讀取這兩個函式的結果，而
            # 這裡原本只處理了 "high"（大盤偏弱，-10分）跟 "warning"（外資賣超，
            # -8分）兩個較輕的等級，完全沒有處理 "extreme" 這個真正該擋新多單的
            # 等級——分數依舊只是照常算、訊號依舊照常推播，"暫停多單" 這個名稱
            # 本身從來沒有真的讓任何一筆多單訊號被擋下來，是純文字訊息、沒接線。
            # 這裡把 "extreme"／action=="stop_buy" 接成真正的硬性攔截：只擋多單
            # （direction=="buy"）方向的新訊號，放空訊號不受影響（大盤重挫/外資
            # 大舉賣超對放空來說反而是順勢，沒有理由一併擋）。
            # get_system_status() 那邊的矛盾另外在該函式做對應修正（不能只看
            # 總分門檻，score>=70 但背後可能是靠其他項目加分蓋過 stop_buy）。
            if direction == "buy" and mc.get("action") == "stop_buy":
                logger.info(f"[{ticker}] 大盤熔斷 extreme（{mc.get('message')}），暫停產生多單訊號")
                _rej("macro_stop"); return None
            if direction == "buy" and fc.get("action") == "stop_buy":
                logger.info(f"[{ticker}] 外資賣超熔斷 extreme（{fc.get('message')}），暫停產生多單訊號")
                _rej("macro_stop"); return None
            if mc.get("level") == "high":
                macro_adj -= 10; macro_notes.append(mc["message"])
            if fc.get("level") == "warning":
                macro_adj -= 8; macro_notes.append(fc["message"])
            elif fc.get("level") == "extreme":
                # 2026-10-06：近100個交易日驗證（/api/analysis/foreign-flow），外資賣超≥50億後
                # 隔天大盤平均+0.02%、5日後平均+2.24%，沒有預測力，改為重扣分不再硬性擋單。
                macro_adj -= 12; macro_notes.append(fc["message"])
            gc_ = check_margin_change(market_overview)
            if gc_.get("triggered"):
                macro_adj -= 8; macro_notes.append(gc_["message"])
            if macro_adj:
                score = max(0, score + macro_adj)
                logger.info(f"[{ticker}] 總經指標評分調整 {macro_adj:+d}：{'；'.join(macro_notes)}")
        except Exception as e:
            logger.warning(f"[{ticker}] 總經指標評分調整失敗（不影響本次訊號，維持原始分數）: {e}")
        if score<THRESH["min_score"]:
            _rej("score_after_adj"); return None
        atr_info=daily_ind.get("atr",{}); atr=atr_info.get("value",price*0.02) or price*0.02
        # ★ 新增：2026-10-05——追高防線。實查 9/30~10/1 六檔訊號：進場價就是訊號當日
        # 收盤價，其中 南茂 +9.6%（接近漲停）、艾笛森 +4.0%、台泥 +4.0%，全部是「已經
        # 噴出之後才被趨勢指標確認」的標的——EMA多頭/站上半年線/MACD/量增/ADX這些指標
        # 全部在測量同一件事：「已經漲了」，所以分數會一路貼頂（95~97）而沒有鑑別力，
        # 也沒有任何一項在檢查「現在買的位置好不好」。隔天一回檔（或高開低走）就碰到
        # 停損。這裡補兩道關卡（只對做多；做空鏡像）：
        #   1) 當日漲幅 >= 6.5%：貼近漲停，隔日開高難成交、回測機率高，直接略過；
        #   2) 與 20 日均線的乖離 > 3 ATR：延伸過度，等回檔再說，直接略過；
        # 並算出 ext_atr（延伸度）供排序降權與前端顯示「追高風險」。
        chg_today = daily_data.get("change_pct", 0) or 0
        e_mid = (daily_ind.get("ema", {}) or {}).get("e_mid")
        if e_mid and atr:
            ext_atr = round(((price - e_mid) if direction == "buy" else (e_mid - price)) / atr, 2)
        else:
            ext_atr = 0
        chg_dir = chg_today if direction == "buy" else -chg_today
        if chg_dir >= 6.5:
            logger.info(f"[{ticker}] 當日{'漲' if direction=='buy' else '跌'}幅 {chg_dir:.1f}% 接近漲跌停，追價風險過高，略過")
            _rej("chase_limit"); return None
        if ext_atr > 3.0:
            logger.info(f"[{ticker}] 與20日均線乖離 {ext_atr:.1f} ATR，延伸過度，等回檔再說，略過")
            _rej("extension"); return None
        chase_pen = 0
        if chg_dir >= 4: chase_pen += 6
        if ext_atr > 2.0: chase_pen += 6
        chase_level = "high" if (chg_dir >= 4 or ext_atr > 2.5) else "mid" if (chg_dir >= 2.5 or ext_atr > 1.5) else "low"
        sl=calc_stop_loss_tw(direction,price,atr,daily_ind,size_cat,low_5d,high_5d)
        # ★ 新增：2026-09-16——使用者要求逐一核對歷史訊號的實際結果，核對時發現
        # 全友(2305.TW) 09-09/09-11/09-14 三次進場，SL 都是同一個 33.9（結構性
        # 支撐位 nearest_support，不是隨每天股價重算的 ATR 停損），但同一段時間
        # 股價從 35 噴到 45，停損距離從約 3% 悄悄擴大到近 25%——calc_stop_loss_tw()
        # 裡 low_5d/nearest_support 的「往外擴」保護（見上方函式），設計原意是
        # 避免停損卡在雜訊區間，但沒有對「擴大後的停損距離」設任何上限，遇到
        # 股價已經噴出、離結構性支撐很遠的個股，就會算出一個風險已經沒有意義
        # 的巨大停損（TP1都要漲快50%才會到），這種訊號與其說是「風險定義清楚的
        # 波段進場點」，不如說是「追高」。這裡補一道明確上限：停損距離超過現價
        # 12%，代表這檔已經噴出太遠、不是好的風險定義進場點，直接跳過不出訊號，
        # 而不是硬塞一個名義上有停損、實際上跟樂透差不多的訊號。
        sl_dist_pct = abs(price - sl) / price * 100 if price else 0
        if sl_dist_pct > 12:
            logger.info(f"[{ticker}] 停損距離 {sl_dist_pct:.1f}%（SL={sl}, 現價={price}）過寬，"
                        f"代表已離結構性支撐/壓力太遠、不是好的風險定義進場點，跳過本次訊號")
            _rej("sl_wide", sl); return None
        tp_info=calc_take_profits_tw(direction,price,sl,size_cat)
        if tp_info["rr1"]<THRESH["min_rr"]:
            _rej("rr_low", sl); return None
        pos=calc_position_size(price,sl,size_cat=size_cat,avg_volume_lots=stock_info.get("volume_lots"))
        if pos["shares"]<=0:
            _rej("size_zero", sl); return None
        if pos.get("liquidity_capped"):
            logger.info(f"[{ticker}] 建議部位因流動性上限（當日成交量10%）被下修至 {pos['shares']} 股")
        ema_ind=daily_ind.get("ema",{})
        ema5_val=ema_ind.get("e_fast") if ema_ind.get("valid") else None
        # ★ 修正：2026-09-16——稽核發現這裡原本不分方向，buy/sell 都套同一組公式
        # entry_zone=[price*0.98, price*1.005]（在目前價格下方回檔進場）。對 buy
        # 是合理的（拉回進場），但對 sell（做空）完全反了——這個區間叫使用者在
        # 「已經比現價低2%」的價位去放空，等於叫人追跌放空，跟正常的「反彈到
        # 壓力區再空」邏輯相反。這裡補上方向判斷，sell 的進場區間改成現價上方
        # （反彈進場）。
        if direction=="buy":
            entry_zone_low=round(min(price*0.98, ema5_val*0.99) if ema5_val else price*0.98, 2)
            entry_zone_high=round(price*1.005,2)
        else:
            entry_zone_low=round(price*0.995,2)
            entry_zone_high=round(max(price*1.02, ema5_val*1.01) if ema5_val else price*1.02, 2)
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
                     f"【位置】當日{chg_today:+.1f}%｜距20日線 {ext_atr:.1f} ATR｜追高風險：{ {'low':'低','mid':'中','high':'高'}[chase_level] }\n"
                     f"【風控】止損 {round(abs(price-sl)/price*100,1)}%（{abs(price-sl)/atr:.1f} ATR），TP1 盈虧比 1:{tp_info['rr1']}"
                     + ("\n【流動性】建議部位已因當日成交量偏低而下修，請留意實際下單時的滑價" if pos.get("liquidity_capped") else ""))
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
            "atr_pct":round(atr/price*100,2),"sl_atr":round(abs(price-sl)/atr,2) if atr else None,
            "ext_atr":ext_atr,"chase_level":chase_level,"chase_pen":chase_pen,
            "inst_signal":inst_signal,"weekly_bias":mtf["weekly_bias"],
            "reason_brief":reason_brief,"reason_full":reason_full,
            "conditions_met":mtf["conditions_met"],"conditions_fail":mtf["conditions_fail"],
            "generated_at":datetime.now(timezone.utc).isoformat(),"expire_days":CB["signal_expire_days"],
            "result":"pending","pnl_twd":0,"status":"active",
            # ★ 新增：2026-09-28——使用者要求親自詢問 ChatGPT/Perplexity 目前系統
            # 還缺什麼，Perplexity 給的立即可部署建議：在完整的「資料三態化」
            # 工程做完之前，先用一個很小的修補把放空訊號明確標成「研究版／
            # 不可執行」，因為系統目前完全沒有借券可得性、借券費率、強制回補、
            # 軋空風險這些做空真正需要的資料（見 fetch_market_regime() 附近
            # 對放空的其他討論），而且新的收緊門檻本身也還沒經過回測驗證
            # （見 config.py SHORT_SIGNAL_THRESH 上方說明）。這裡不是要阻止
            # 系統產生放空訊號（使用者仍想看到這些訊號，用來觀察/研究），
            # 而是讓 Telegram 推播明確告知使用者「這則不是跟做多同等級的
            # 可執行建議」，不要在沒有這些資料前被當一般訊號直接下單。
            "executable": direction != "sell",
            "short_research_only_reason": (
                "放空門檻(分數75/ADX方向確認/週線硬性偏空/大盤中期regime)尚未經過"
                "真實成交資料回測驗證；且系統目前沒有借券可得性、借券費率、"
                "強制回補日、軋空風險等做空必要資料"
            ) if direction == "sell" else None,
        }
        logger.info(f"[{ticker}] ✅ {name} {direction} score={score}({grade}) SL={sl:.1f} TP1={tp_info['tp1']:.1f} shares={pos['shares']}")
        return signal
    except Exception as e:
        logger.error(f"generate_signal_tw {ticker}: {e}", exc_info=True); return None
