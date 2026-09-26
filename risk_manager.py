"""
risk_manager.py — 台股波段版 v1.0
移除：EIA排程、check_earnings_risk、check_weekend_gap
修正：check_vix → check_market_circuit_breaker（大盤跌幅）
新增：check_foreign_flow、台股張數風控
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List
from config import CIRCUIT_BREAKER as CB, ACCOUNT_BALANCE_TWD, MAX_SIMULTANEOUS_POSITIONS, MAX_DAILY_RISK

logger = logging.getLogger(__name__)

# ★ 修正：2026-09-26（稽核發現，回應「fail-open 比模型不準更危險」的問題）——
# check_market_circuit_breaker / check_foreign_flow / check_margin_change 這三個
# 熔斷輸入原本在資料缺失時，都是安靜地把缺的值當成一個「看起來正常」的預設值
# （twii_chg=0、vix=20、net_buy_twd=0、chg_pct=0），下面的判斷完全無法區分
# 「市場真的平盤/沒事」跟「上游資料根本沒抓到」，等於把「不知道」直接偽裝成
# 「沒事」回傳給呼叫端。這裡統一補上 data_available 欄位：用「這個來源字典裡
# 有沒有預期的 key」而不是「值是不是 0」來判斷資料是否真的存在，讓
# get_system_status() 與訊號推播都能看見「這次判斷是建立在缺資料之上」，
# 而不是被無聲吞掉。level 也新增 "unknown"，跟既有的 extreme/high/warning/
# normal/positive 分開，避免呼叫端誤把「不知道」當「normal」處理。
def check_market_circuit_breaker(market_overview: Dict) -> Dict:
    idx=market_overview.get("index",{}); twii=idx.get("twii",{}); vix_d=idx.get("vix",{})
    twii_available = bool(twii) and twii.get("source") != "error"
    vix_available   = bool(vix_d) and vix_d.get("source") != "error"
    if not twii_available:
        logger.warning("check_market_circuit_breaker: market_overview 缺少 twii 資料，twii_chg 會當作 0% 處理，並標記 data_available=False")
    if not vix_available:
        logger.warning("check_market_circuit_breaker: market_overview 缺少 vix 資料，vix 會當作 20 處理，並標記 data_available=False")
    data_available = twii_available and vix_available
    twii_chg=float(twii.get("chg",0) or 0); vix=float(vix_d.get("price",20) or 20)
    if twii_chg<=CB["twii_drop_stop"] or vix>=CB["vix_extreme"]:
        return {"triggered":True,"level":"extreme","twii_chg":twii_chg,"vix":vix,"data_available":data_available,
                "message":f"🚨 大盤重挫 {twii_chg:.1f}% / VIX {vix:.0f}，暫停所有多單","action":"stop_buy"}
    if twii_chg<=CB["twii_drop_caution"] or vix>=CB["vix_high"]:
        return {"triggered":True,"level":"high","twii_chg":twii_chg,"vix":vix,"data_available":data_available,
                "message":f"⚠️ 大盤偏弱 {twii_chg:.1f}%，謹慎操作","action":"reduce_confidence"}
    if not data_available:
        return {"triggered":False,"level":"unknown","twii_chg":twii_chg,"vix":vix,"data_available":False,
                "message":"⚠️ 大盤/VIX 資料目前取不到，熔斷判斷可能不可靠"}
    return {"triggered":False,"level":"normal","twii_chg":twii_chg,"vix":vix,"data_available":True}

def check_foreign_flow(market_overview: Dict) -> Dict:
    foreign=market_overview.get("foreign",{})
    data_available="net_buy_twd" in foreign
    nb=float(foreign.get("net_buy_twd",0) or 0)
    if not data_available:
        logger.warning("check_foreign_flow: market_overview 缺少 foreign 資料，net_buy_twd 會當作 0 處理，並標記 data_available=False")
    if nb<=CB["foreign_sell_stop"]:
        return {"triggered":True,"level":"extreme","net_buy_twd":nb,"data_available":data_available,
                "message":f"🚨 外資賣超 {abs(nb)/1e8:.0f}億，暫停多單","action":"stop_buy"}
    if nb<=CB["foreign_sell_caution"]:
        return {"triggered":True,"level":"warning","net_buy_twd":nb,"data_available":data_available,
                "message":f"⚠️ 外資賣超 {abs(nb)/1e8:.0f}億，降低信心度","action":"reduce_confidence"}
    if not data_available:
        return {"triggered":False,"level":"unknown","net_buy_twd":nb,"data_available":False,
                "message":"⚠️ 外資買賣超資料目前取不到"}
    if nb>=20e8:
        return {"triggered":False,"level":"positive","net_buy_twd":nb,"data_available":True,"message":f"✅ 外資買超 {nb/1e8:.0f}億，市場偏多"}
    return {"triggered":False,"level":"normal","net_buy_twd":nb,"data_available":True}

# ★ 新增：2026-09-24——見 data_fetcher.fetch_margin_change() 說明：
# CB["margin_change_warning"] 原本只是設定檔裡一個從未被使用的數字，這裡
# 補上對應的檢查函式，讓融資餘額急縮這個總經指標真正影響訊號信心度
# （score_adj），跟既有的大盤漲跌/外資買賣超走同一套「熔斷=擋新單、
# 警告=降低信心度」邏輯，而不是又額外發明一套規則。
# ★ 修正：2026-09-26（稽核發現）——同上，補 data_available 判斷，見本函式群
# 開頭的說明。
def check_margin_change(market_overview: Dict) -> Dict:
    margin = market_overview.get("margin", {})
    data_available = "chg_pct" in margin
    chg = float(margin.get("chg_pct", 0) or 0)
    warn_th = CB.get("margin_change_warning", -5.0)
    if not data_available:
        logger.warning("check_margin_change: market_overview 缺少 margin 資料，chg_pct 會當作 0% 處理，並標記 data_available=False")
    if chg <= warn_th:
        return {"triggered": True, "level": "warning", "chg_pct": chg, "data_available": data_available,
                "message": f"⚠️ 融資餘額單日減少 {abs(chg):.1f}%，市場信心轉弱", "action": "reduce_confidence"}
    if not data_available:
        return {"triggered": False, "level": "unknown", "chg_pct": chg, "data_available": False,
                "message": "⚠️ 融資餘額資料目前取不到"}
    return {"triggered": False, "level": "normal", "chg_pct": chg, "data_available": True}

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
    # ★ 修正：2026-09-26（稽核 finding #4）——原本這裡寫死字面常數 0.06，
    # 跟 config.py 的 MAX_DAILY_RISK=0.06 只是「數字剛好相同」，兩者沒有真的
    # 連在一起：以後如果改 config.py 的每日虧損上限，這裡完全不會反應，
    # 形成一個「看起來可調、實際上調了沒用」的陷阱（跟 signal_engine.
    # calc_position_size() 2026-09-16 修正過的同一類問題）。改成直接從
    # config 讀，兩處數字保證永遠一致。
    max_daily=ACCOUNT_BALANCE_TWD*MAX_DAILY_RISK
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
        "market":       check_market_circuit_breaker(market_overview),
        "foreign":      check_foreign_flow(market_overview),
        "margin_chg":   check_margin_change(market_overview),
        "spike":        check_price_spike(ticker,tf_data),
        "account":      check_account_requirement(ticker,stock_info),
        "daily_loss":   check_daily_loss_limit(),
        "max_pos":      check_max_positions(active_signals),
        "margin":       check_margin_usage(active_signals),
    }
    warnings=[]; blockers=[]; score_adj=0
    if checks["market"].get("level")=="extreme":  blockers.append(checks["market"]["message"])
    if checks["foreign"].get("level")=="extreme": blockers.append(checks["foreign"]["message"])
    if checks["daily_loss"].get("exceeded"):      blockers.append(checks["daily_loss"]["message"])
    if checks["max_pos"].get("exceeded"):         blockers.append(checks["max_pos"]["message"])
    if checks["spike"].get("spike"):              blockers.append(checks["spike"]["message"])
    if checks["market"].get("level")=="high":     warnings.append(checks["market"]["message"]); score_adj-=10
    if checks["foreign"].get("level")=="warning": warnings.append(checks["foreign"]["message"]); score_adj-=8
    if checks["margin_chg"].get("triggered"):     warnings.append(checks["margin_chg"]["message"]); score_adj-=8
    if not checks["account"].get("sufficient"):   warnings.append(checks["account"]["message"]); score_adj-=20
    if checks["margin"].get("warning"):           warnings.append(checks["margin"]["message"]); score_adj-=5
    # ★ 新增：2026-09-26（稽核發現，fail-closed 補強）——market/foreign/margin_chg
    # 這三個熔斷輸入若標記 level=="unknown"（資料抓不到，見上面各 check_* 函式的
    # data_available 判斷），代表這筆訊號的總經/風控判斷是建立在不完整資料上。
    # 這裡不直接擋單（這些本來就是輔助性的總經疊加，不是唯一進場依據，稽核報告
    # s3 也指出目前疊加對回測沒有展現可測量改善，貿然全部改成 blocker 過度激進），
    # 但至少扣一點信心分並讓警告訊息可見，不再讓「不知道」被完全靜默吞成「沒事」。
    for _k in ("market","foreign","margin_chg"):
        if checks[_k].get("level")=="unknown":
            warnings.append(checks[_k]["message"]); score_adj-=3
    return {"status":"blocked" if blockers else "warning" if warnings else "clear",
            "warnings":warnings,"blockers":blockers,"checks":checks,"score_adj":score_adj,"can_signal":len(blockers)==0}

def get_system_status(market_overview: Dict) -> Dict:
    market_cb=check_market_circuit_breaker(market_overview)
    foreign=check_foreign_flow(market_overview)
    twii_chg=market_cb.get("twii_chg",0); vix=market_cb.get("vix",20); score=100
    if market_cb.get("level")=="extreme":   score-=50; st="大盤重挫"; cl="red"
    elif market_cb.get("level")=="high":    score-=25; st="大盤偏弱"; cl="orange"
    elif market_cb.get("level")=="unknown": st="大盤資料異常"; cl="orange"
    elif twii_chg>1.0:                      score+=10; st="大盤強勢"; cl="green"
    else:                                   st="正常";  cl="green"
    if foreign.get("level")=="extreme":    score-=20
    elif foreign.get("level")=="warning":  score-=10
    elif foreign.get("level")=="positive": score+=10
    daily=check_daily_loss_limit(); score=max(0,min(100,score))
    cat_advice={}
    for cat in ["ETF","半導體","AI概念","金融保險","航運","生技醫療"]:
        cat_advice[cat]="✅ 適合交易" if score>=70 else "⚠️ 謹慎" if score>=40 else "🔴 觀望"
    # ★ 新增：2026-09-26（稽核發現）——原本這裡完全沒有揭露「這次環境評分是不是
    # 建立在缺資料上」，儀表板/使用者只會看到一個看起來正常的分數，不知道背後
    # 大盤或外資資料其實剛好抓不到。data_issues 讓前端可以額外提示。
    data_issues=[m for m in (market_cb.get("message") if market_cb.get("level")=="unknown" else None,
                              foreign.get("message") if foreign.get("level")=="unknown" else None) if m]
    return {"env_score":score,"env_status":st,"env_color":cl,"twii_chg":twii_chg,"vix":vix,
            "can_trade":score>=50,"category_advice":cat_advice,"daily_loss":daily,
            "foreign_signal":foreign.get("message",""),"data_issues":data_issues}
