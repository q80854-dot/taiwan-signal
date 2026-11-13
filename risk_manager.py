"""
risk_manager.py — 台股波段版 v1.0
移除：EIA排程、check_earnings_risk、check_weekend_gap
修正：check_vix → check_market_circuit_breaker（大盤跌幅）
新增：check_foreign_flow、台股張數風控
"""
import logging
import threading
import pytz
from datetime import datetime
from typing import Dict, List
from config import CIRCUIT_BREAKER as CB, ACCOUNT_BALANCE_TWD, MAX_SIMULTANEOUS_POSITIONS, MAX_DAILY_RISK, MAX_PORTFOLIO_RISK, MAX_RISK_PER_TRADE

logger = logging.getLogger(__name__)


# ★ 2026-11-11 LIVE03：風險上限的單一事實來源。三個語意不同、不可混用的上限，各頁一律讀此處、
# 不得各自硬編（審查 §9.5 LIVE03：各頁把單日 6% 當組合上限、歷史訊號 1.2% 與設定頁 2% 混淆）。
_RISK_EFFECTIVE_AT = "2026-11-12"   # 現行風控/Champion 凍結日（CHAMPION_SNAPSHOT.frozen_at）


def open_risk_cap() -> float:
    """★ 2026-11-13（外部審查 P0）正式發訊流程實際生效的組合風險上限：「全部未平倉部位＋本次新單」的停損
    風險合計，不得超過帳戶資金的這個比例。scanner 的資金閘門與組合回測都只讀這個函式——原本正式流程用 6%
    （MAX_DAILY_RISK）、組合回測預設卻用 15%（MAX_PORTFOLIO_RISK），兩邊持倉集合與曝險不同，回測不等於正式策略。
    15% 的 MAX_PORTFOLIO_RISK 是「最壞同時停損」的硬上限，語意不同、目前未接入發訊流程（見 risk_basis）。"""
    return MAX_DAILY_RISK


def risk_basis(balance: float = None) -> Dict:
    """風控三上限的權威來源，供所有頁面/端點引用（禁止各自硬編）。
      - per_trade_pct        單筆預算：每筆停損時最多虧本金的比例（MAX_RISK_PER_TRADE）
      - daily_new_pct        單日新增：當日所有『新』單的風險總和上限（MAX_DAILY_RISK）
      - portfolio_hard_cap_pct 組合硬上限：所有部位『最壞同時停損』總和的硬上限，超過即暫停新單（MAX_PORTFOLIO_RISK）
    三者語意不同、不可互相代用；歷史訊號的風險%為其產生當時的實算值，不以現行上限回填。"""
    bal = ACCOUNT_BALANCE_TWD if balance is None else balance
    cfg_ver = None
    try:
        import strategy_version as _sv
        cfg_ver = _sv.config_hash()[:12]
    except Exception:
        pass
    return {
        "per_trade_pct": round(MAX_RISK_PER_TRADE * 100, 2),
        "daily_new_pct": round(MAX_DAILY_RISK * 100, 2),
        "portfolio_hard_cap_pct": round(MAX_PORTFOLIO_RISK * 100, 2),
        "max_positions": MAX_SIMULTANEOUS_POSITIONS,
        "account_balance_twd": bal,
        "config_version": cfg_ver,
        "effective_at": _RISK_EFFECTIVE_AT,
        # ★ 2026-11-12（Q-P1-03）實際生效的檢查點（只描述現況、不改行為）：
        "enforcement": {
            "daily_realized_loss": f"當日已實現虧損累計達本金 {MAX_DAILY_RISK*100:.1f}% → 當日暫停新訊號"
                                   "（掃描前閘門；狀態讀不到時同樣暫停）",
            "open_risk_budget": f"發訊號前：既有未平倉部位的停損風險＋新訊號風險合計 ≤ 本金 {MAX_DAILY_RISK*100:.1f}%"
                                "（scanner 以 fit_to_cash 縮股數或不發）——此上限涵蓋『全部未平倉』，不只當日新單",
            "portfolio_hard_cap": f"{MAX_PORTFOLIO_RISK*100:.0f}% 硬上限僅用於組合風險面板的警示；因 open_risk_budget "
                                  "更嚴，正式發訊流程不會先觸及此上限（check_margin_usage 未接入發訊流程）",
        },
        "basis": "三個上限語意不同、不可混用：單筆預算＝每筆停損最多虧本金比例；單日新增＝當日所有新單"
                 "風險總和上限；組合硬上限＝所有部位最壞同時停損總和的硬上限（超過即暫停新單、不動既有部位）。"
                 "參數隨程式部署生效（effective_at 為現行凍結日）；歷史訊號的風險%為產生當時的實算值，不回填現行上限。",
    }

# ★ 修正：2026-09-29（跨AI覆核發現）——record_signal_loss / check_daily_loss_limit
# 原本用 datetime.now(timezone.utc) 算「今天」，但 app.py 全部排程（包含真正會
# 呼叫 record_signal_loss() 的 scanner.intraday_check/daily_scan）都是用
# Asia/Taipei 時區安排的。雖然目前所有實際觸發時間換算成UTC都還在同一個UTC
# 日曆日內，不會踩到邊界，但 /api/state 是前端每30秒直接輪詢
# check_daily_loss_limit()，在台北時間00:00–08:00（對應前一個UTC日）這段
# 期間會誤判成「今天」還沒開始、把昨天其實還有效的虧損歸零顯示。統一改成
# 跟 app.py 排程同一個 TZ_TAIPEI，避免這個日期定義不一致的陷阱。
TZ_TAIPEI = pytz.timezone("Asia/Taipei")

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
    # ★ 2026-11-08 使用者核准、依台股實際量能重校（見 config.CIRCUIT_BREAKER 說明）：
    # 三層——extreme(≥1,800億)=史上前幾大恐慌日，暫停新多單（action=stop_buy，由上層硬擋）；
    # heavy(≥1,000億)=大幅降低信心（扣12分）；warning(≥500億)=降低信心（扣8分）。放空不受影響。
    if nb<=CB["foreign_sell_stop"]:
        return {"triggered":True,"level":"extreme","net_buy_twd":nb,"data_available":data_available,
                "message":f"🚨 外資賣超 {abs(nb)/1e8:.0f}億（史上極端等級），暫停新多單","action":"stop_buy"}
    if nb<=CB["foreign_sell_heavy"]:
        return {"triggered":True,"level":"heavy","net_buy_twd":nb,"data_available":data_available,
                "message":f"🚨 外資賣超 {abs(nb)/1e8:.0f}億，大幅降低信心度","action":"reduce_confidence"}
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

# ★ 修正：2026-10-09——每日虧損熔斷原本只存在記憶體（_daily_loss），Render 一重新部署／
# 休眠喚醒就歸零，等於 6% 單日停損保護在重啟後形同虛設。改成同時寫入資料庫（meta
# risk_daily_loss），讀取時以資料庫為準、記憶體為備援，確保跨重啟仍能累計當日虧損。
# ★ 修正：2026-11-04（F-07 fail-closed）——原本 _load_daily() 無論資料庫讀取成功、key
# 不存在、還是「讀取失敗」都走同一條退路回記憶體值；而記憶體在 Render 重啟後是空的
# （date=""），於是「資料庫讀不到 ＋ 進程剛重啟」時，當日累計虧損會被無聲歸零，6% 單日
# 停損保護等於失效（fail-open）。改用 store.read_meta() 區分三種狀態：讀到就用、key 不
# 存在＝今天還沒有虧損紀錄（合法）、讀取失敗＝風控狀態未知（標記 unavailable，交由
# check_daily_loss_limit / run_all_checks 以 fail-closed 擋新單）。
def _load_daily() -> Dict:
    try:
        from state_store import store
        status, d = store.read_meta("risk_daily_loss")
    except Exception as e:
        logger.error(f"_load_daily 讀取風控狀態失敗：{e}")
        return {**_daily_loss, "unavailable": True}
    if status == "error":
        logger.error("_load_daily：資料庫讀取失敗，當日虧損狀態未知，將 fail-closed 擋新單")
        return {**dict(_daily_loss), "unavailable": True}
    if status == "ok" and isinstance(d, dict) and d.get("date"):
        return {"date": d.get("date", ""), "loss_twd": float(d.get("loss_twd") or 0),
                "signal_count": int(d.get("signal_count") or 0)}
    # status == "missing"（或 ok 但無當日資料）：合法的「今天尚無虧損紀錄」
    return dict(_daily_loss)

def _save_daily(d: Dict):
    _daily_loss.update(d)
    try:
        from state_store import store
        if not store.set_meta("risk_daily_loss", d):
            # ★ F-07：寫入失敗要可見，不可當成功。記憶體仍更新作為當次進程的備援，
            # 但一旦重啟就會遺失，因此以 error 等級記錄，便於告警/排查。
            logger.error("_save_daily：每日虧損寫入資料庫失敗，重啟後將遺失當日累計（請檢查 DB）")
    except Exception as e:
        logger.error(f"_save_daily：寫入每日虧損發生例外：{e}")

# ★ 修正：2026-11-04（驗收清單「兩執行緒同時累加→不遺失紀錄」）——record_signal_loss 是
# 讀取→修改→寫回的流程，多執行緒（排程掃描＋前端輪詢觸發）同時進來會互相覆蓋而遺失累計。
# 本服務為單一 worker，用行程內鎖把整段 read-modify-write 串行化即可保證不遺失。
_daily_lock = threading.Lock()

def record_signal_loss(loss_twd: float):
    today=datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    with _daily_lock:
        cur=_load_daily()
        if cur.get("unavailable"):
            # 風控狀態讀取失敗：不可在未知基準上累加（會得到錯誤的當日虧損），直接略過本次累計，
            # 由 fail-closed 的 check_daily_loss_limit 擋新單；待資料庫恢復再正常累計。
            logger.error("record_signal_loss：風控狀態讀取失敗，略過本次累計（fail-closed 由 check 擋單）")
            return
        if cur.get("date")!=today:
            cur={"date":today,"loss_twd":0.0,"signal_count":0}
        if loss_twd<0: cur["loss_twd"]+=abs(loss_twd)
        cur["signal_count"]+=1
        _save_daily(cur)

def check_daily_loss_limit() -> Dict:
    # ★ 修正：2026-09-26（稽核 finding #4）——原本這裡寫死字面常數 0.06，
    # 跟 config.py 的 MAX_DAILY_RISK=0.06 只是「數字剛好相同」，兩者沒有真的
    # 連在一起：以後如果改 config.py 的每日虧損上限，這裡完全不會反應，
    # 形成一個「看起來可調、實際上調了沒用」的陷阱（跟 signal_engine.
    # calc_position_size() 2026-09-16 修正過的同一類問題）。改成直接從
    # config 讀，兩處數字保證永遠一致。
    max_daily=ACCOUNT_BALANCE_TWD*MAX_DAILY_RISK
    today=datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    cur=_load_daily()
    # ★ F-07 fail-closed：風控狀態讀取失敗（資料庫未知）時，不可假裝「今日無虧損、可交易」，
    # 回報 unavailable 讓 run_all_checks 擋新單。
    if cur.get("unavailable"):
        return {"exceeded":False,"unavailable":True,"today_loss":None,"max_loss":round(max_daily,0),
                "message":"🔴 風控狀態暫時無法讀取（資料庫），為安全起見暫停新增訊號"}
    if cur.get("date")!=today:
        return {"exceeded":False,"today_loss":0,"max_loss":round(max_daily,0),"remaining":round(max_daily,0)}
    loss=cur["loss_twd"]
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

def plan_rescale(open_rows: List[Dict], balance: float = None, max_positions: int = None, max_risk: float = None) -> List[Dict]:
    """2026-10-28：依新部位規則重算追蹤中部位的股數（只會縮小、不會放大）。
    依發出時間由舊到新分配：每檔 ≤ 資金 ÷ 同時持倉上限、進場成本合計 ≤ 資金、停損風險合計 ≤ 資金 × MAX_DAILY_RISK。
    回傳需要變更的清單 [{id, code, before, after, risk_twd, risk_pct, position_value}]；after=0 代表放不下（保留原樣交由使用者判斷）。"""
    import math
    bal = balance or ACCOUNT_BALANCE_TWD
    mp = max(1, max_positions or MAX_SIMULTANEOUS_POSITIONS)
    cash, risk_left = bal, bal * (max_risk if max_risk is not None else MAX_DAILY_RISK)
    out = []
    for r in sorted(open_rows or [], key=lambda x: str(x.get("generated_at") or "")):
        px = float(r.get("entry_price") or 0); sh = int(r.get("suggested_shares") or r.get("suggested_lots") or 0)
        rps = abs(px - float(r.get("stop_loss") or px))
        if px <= 0 or sh <= 0:
            continue
        lim = min(sh, math.floor(bal / mp / px), math.floor(max(0.0, cash) / px))
        if rps > 0:
            lim = min(lim, math.floor(max(0.0, risk_left) / rps))
        lim = max(0, lim)
        if lim >= 1:
            cash -= lim * px; risk_left -= lim * rps
        if lim != sh:
            out.append({"id": r.get("id"), "code": r.get("code"), "before": sh, "after": lim,
                        "risk_twd": round(lim * rps, 0), "risk_pct": round(lim * rps / bal * 100, 2),
                        "position_value": round(lim * px, 0)})
    return out

# ★ 修正：2026-11-04（F-09）——組合總風險原本只有 10% 的「警告」、never 擋單。補上「硬上限」
# （config.MAX_PORTFOLIO_RISK，預設 15%）：所有進行中部位的「最壞同時停損」總和超過此比例時，
# 回 exceeded=True，由 run_all_checks 當 blocker 擋掉新訊號（只擋新單、不動既有部位）。
def check_margin_usage(active_signals: List[Dict]) -> Dict:
    total_risk_pct=sum(s.get("risk_pct",0) for s in active_signals)
    total_risk_twd=sum(s.get("risk_twd",0) for s in active_signals)
    total_value=sum(s.get("position_value",0) for s in active_signals)
    portfolio_pct=round(total_value/ACCOUNT_BALANCE_TWD*100,1) if ACCOUNT_BALANCE_TWD else 0
    hard=MAX_PORTFOLIO_RISK*100
    base={"total_risk_pct":round(total_risk_pct,1),"total_risk_twd":round(total_risk_twd,0),
          "portfolio_pct":portfolio_pct,"hard_limit_pct":round(hard,1)}
    if total_risk_pct>hard:
        return {**base,"warning":True,"exceeded":True,
                "message":f"🔴 組合總風險 {total_risk_pct:.1f}% 超過硬上限 {hard:.0f}%，暫停新增訊號（不影響既有部位）"}
    if total_risk_pct>10:
        return {**base,"warning":True,"exceeded":False,"message":f"⚠️ 組合總風險 {total_risk_pct:.1f}%，接近組合硬上限 {hard:.0f}%（所有部位最壞同時停損總和）"}
    return {**base,"warning":False,"exceeded":False}

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
    if checks["daily_loss"].get("exceeded"):      blockers.append(checks["daily_loss"]["message"])
    # ★ F-07 fail-closed：每日虧損風控狀態未知（資料庫讀取失敗）＝不可放行新單
    if checks["daily_loss"].get("unavailable"):   blockers.append(checks["daily_loss"]["message"])
    if checks["max_pos"].get("exceeded"):         blockers.append(checks["max_pos"]["message"])
    if checks["spike"].get("spike"):              blockers.append(checks["spike"]["message"])
    # ★ F-09：組合總風險超過硬上限＝擋新單（只擋新增，不影響既有部位）
    if checks["margin"].get("exceeded"):          blockers.append(checks["margin"]["message"])
    if checks["market"].get("level")=="high":     warnings.append(checks["market"]["message"]); score_adj-=10
    # 外資賣超三層（2026-11-08 重校，見 config.CIRCUIT_BREAKER）：warning −8 / heavy −12 /
    # extreme（暫停等級）−12。做多的方向感知硬性攔截在 signal_engine（fc.action=="stop_buy"）；
    # 本函式方向無關、且為輔助性總經疊加，故 extreme 以重扣分呈現、不直接 blocker（避免誤擋放空）。
    if checks["foreign"].get("level")=="warning":   warnings.append(checks["foreign"]["message"]); score_adj-=8
    elif checks["foreign"].get("level")=="heavy":   warnings.append(checks["foreign"]["message"]); score_adj-=12
    elif checks["foreign"].get("level")=="extreme": warnings.append(checks["foreign"]["message"]); score_adj-=12
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

def _twii_ok(market_overview: Dict) -> bool:
    t=(market_overview.get("index") or {}).get("twii") or {}
    return bool(t) and t.get("source")!="error"

def get_system_status(market_overview: Dict) -> Dict:
    market_cb=check_market_circuit_breaker(market_overview)
    foreign=check_foreign_flow(market_overview)
    twii_chg=market_cb.get("twii_chg",0); vix=market_cb.get("vix",20); score=100
    if market_cb.get("level")=="extreme":   score-=50; st="大盤重挫"; cl="red"
    elif market_cb.get("level")=="high":    score-=25; st="大盤偏弱"; cl="orange"
    elif market_cb.get("level")=="unknown":
        # ★ F-08（2026-11-04）：資料不完整不可顯示成乾淨滿分。大盤(TWII)本身抓不到＝
        # 完全無法判斷（下方 market_unknown 會把分數設 None、不可交易）；只缺 VIX＝熔斷的
        # VIX 極端保護暫時盲目，仍可參考但要明確扣分並標示，不再出現「100 分／VIX 缺漏／
        # 適合交易」自相矛盾的畫面。
        if not _twii_ok(market_overview): st="大盤資料異常"
        else: st="VIX 資料缺漏（熔斷判斷不完整）"; score-=20
        cl="orange"
    elif twii_chg>1.0:                      score+=10; st="大盤強勢"; cl="green"
    else:                                   st="正常";  cl="green"
    if foreign.get("level")=="extreme":    score-=20
    elif foreign.get("level")=="heavy":    score-=15
    elif foreign.get("level")=="warning":  score-=10
    elif foreign.get("level")=="positive": score+=10
    daily=check_daily_loss_limit(); score=max(0,min(100,score))
    # 2026-10-08：大盤資料抓不到時原本仍以 100 分起算，畫面出現「100／大盤資料異常」且可交易＝是。
    # 沒有資料就不給分數（None，前端顯示「—」），也不判定可交易。
    market_unknown = market_cb.get("level")=="unknown" and not _twii_ok(market_overview)
    if market_unknown: score=None
    # ★ F-08：只缺 VIX（TWII 正常）＝資料不完整但仍可保守參考，不整個停判
    vix_missing = market_cb.get("level")=="unknown" and _twii_ok(market_overview)
    # ★ 修正：2026-09-29——使用者回報這頁同一畫面「外資賣超632億，暫停多單」
    # 跟「可交易：是」「每個產業✅適合交易」同時出現，互相矛盾。根因：
    # can_trade 跟 cat_advice 原本純粹看 score 門檻（>=50 / >=70），但 score
    # 只是把 market_cb/foreign 的等級各自扣固定分數後加總——大盤重挫(extreme,
    # -50)同時外資也極端賣超(extreme,-20)才會扣到70分以下，只有外資單獨
    # extreme（-20分，100→80分）並不會把總分拉到70以下，可交易/適合交易的
    # 文字判斷完全沒有意識到「stop_buy」這個訊號本身就是要暫停多單，只把它
    # 當成一般扣分項目——這正是 signal_engine.py 那邊「extreme 等級原本也只
    # 是扣分、沒有真的擋單」的同一個根因，只是這裡是顯示層版本。
    # 這裡讓 can_trade／cat_advice 直接看 market_cb/foreign 的 action 是否為
    # "stop_buy"，是就無條件顯示暫停，不再讓分數門檻蓋過這個訊號。
    stop_buy = market_cb.get("action")=="stop_buy" or foreign.get("action")=="stop_buy"
    # ★ F-07/F-08：每日虧損風控狀態未知（資料庫讀取失敗）時，儀表板也不可顯示「可交易」，
    # 與 run_all_checks 的 fail-closed 擋單一致。
    risk_unavailable = bool(daily.get("unavailable"))
    can_trade = (not stop_buy) and (not market_unknown) and (not risk_unavailable) and (score or 0)>=50
    cat_advice={}
    for cat in ["ETF","半導體業","電子零組件業","金融保險","航運業","生技醫療業"]:
        if market_unknown:
            cat_advice[cat]="⚠️ 大盤資料異常，暫不判斷"
        elif risk_unavailable:
            cat_advice[cat]="🔴 風控狀態讀取失敗，暫停判斷"
        elif stop_buy:
            cat_advice[cat]="🔴 暫停多單（大盤/外資熔斷中，僅供研究參考）"
        elif vix_missing:
            cat_advice[cat]="⚠️ 謹慎（VIX 資料缺漏，熔斷判斷不完整）"
        else:
            cat_advice[cat]="✅ 適合交易" if score>=70 else "⚠️ 謹慎" if score>=40 else "🔴 觀望"
    # ★ 新增：2026-09-26（稽核發現）——原本這裡完全沒有揭露「這次環境評分是不是
    # 建立在缺資料上」，儀表板/使用者只會看到一個看起來正常的分數，不知道背後
    # 大盤或外資資料其實剛好抓不到。data_issues 讓前端可以額外提示。
    data_issues=[m for m in (market_cb.get("message") if market_cb.get("level")=="unknown" else None,
                              foreign.get("message") if foreign.get("level")=="unknown" else None,
                              daily.get("message") if risk_unavailable else None) if m]
    # ★ PIT 起步 A（2026-11-08）：把既有的資料可用性布林升級成標準化品質狀態（純顯示，
    # 不改 can_trade/stop_buy 的任何判斷）。嚴重度取最差：大盤 TWII 缺＝INVALID（完全無法判斷）、
    # 只缺 VIX＝PARTIAL、外資資料缺＝PARTIAL、每日虧損風控讀取失敗＝INVALID。
    import data_quality as _dq
    data_quality = _dq.worst(
        _dq.INVALID if market_unknown else (_dq.PARTIAL if vix_missing else _dq.VALID),
        _dq.PARTIAL if foreign.get("level")=="unknown" else _dq.VALID,
        _dq.INVALID if risk_unavailable else _dq.VALID,
    )
    return {"env_score":score,"env_status":st,"env_color":cl,"twii_chg":twii_chg,"vix":vix,
            "can_trade":can_trade,"stop_buy":stop_buy,"category_advice":cat_advice,"daily_loss":daily,
            "foreign_signal":foreign.get("message",""),"data_issues":data_issues,
            "data_quality":data_quality}


# ═══ 2026-10-08 持倉滿額時的「極高信心例外」 ═══
# 使用者要求：只有在「百分之百勝率、百分之百信心」時，持倉已超過上限也可以發出通知。
# 現實上任何訊號都不可能保證 100% 勝率，所以這裡把門檻設成「系統自己已平倉的歷史紀錄
# 全部獲利，且樣本數至少 HC_MIN_TRADES 筆」，再加上訊號本身分數要求。這個條件在真實市場
# 幾乎不會成立，所以平常等同於「持倉滿額就不發」；一旦成立也只是多發一則通知，每次掃描最多
# 1 檔，且標示為「超過持倉上限的例外」，不放寬持倉上限本身，是否進場仍由使用者決定。
HC_MIN_TRADES = 30
HC_MIN_SCORE = 90


def high_conf_record(closed_trades: List[Dict]) -> Dict:
    """回傳最近已平倉交易的戰績：{n, wins, perfect}。perfect＝樣本夠多且全部獲利。"""
    rows = [t for t in (closed_trades or []) if t.get("pnl_pct") is not None]
    n = len(rows)
    wins = sum(1 for t in rows if (t.get("pnl_pct") or 0) > 0)
    return {"n": n, "wins": wins, "perfect": n >= HC_MIN_TRADES and wins == n}


def portfolio_risk(open_rows: List[Dict], balance: float = None) -> Dict:
    """★ 2026-11-04（第三方 brief §4#3）：組合層級風險快照——最壞同時停損、曝險、集中度、
    方向曝險、情境壓力測試。純唯讀分析、不下單。以開倉部位的 entry/stop/股數/方向/產業計算；
    市場情境採 beta=1 的簡化假設（部位隨大盤等比變動，明確標示），非精確個股 beta。"""
    bal = balance or ACCOUNT_BALANCE_TWD
    rows = []
    for r in (open_rows or []):
        sh = int(r.get("suggested_shares") or r.get("suggested_lots") or r.get("shares") or 0)
        px = float(r.get("entry_price") or r.get("entry") or 0)
        stop = float(r.get("stop_loss") or r.get("stop") or 0)
        if sh <= 0 or px <= 0:
            continue
        d = "sell" if r.get("direction") == "sell" else "buy"
        rows.append({"code": r.get("code"), "name": r.get("name"), "direction": d,
                     "sector": r.get("sector") or "其他", "value": sh * px,
                     "risk": sh * abs(px - stop) if stop else 0.0})
    n = len(rows)
    pct = lambda v: (round(v / bal * 100, 1) if bal else 0.0)
    if n == 0:
        return {"ok": True, "n": 0, "balance": round(bal, 0), "message": "目前無開倉部位"}
    gross = sum(x["value"] for x in rows)
    long_v = sum(x["value"] for x in rows if x["direction"] == "buy")
    short_v = sum(x["value"] for x in rows if x["direction"] == "sell")
    worst = sum(x["risk"] for x in rows)                      # 全部同時觸停損的最壞損失
    bysec = {}
    for x in rows:
        bysec[x["sector"]] = bysec.get(x["sector"], 0.0) + x["value"]
    sectors = sorted([{"sector": k, "value": round(v, 0), "pct": pct(v)} for k, v in bysec.items()],
                     key=lambda z: -z["value"])
    largest = max(rows, key=lambda x: x["value"])
    scenarios = []
    for d in (3, 5, 10):                                      # 大盤下跌情境（多單虧、空單賺；beta=1 簡化）
        pnl = -(long_v * d / 100) + (short_v * d / 100)
        scenarios.append({"drop_pct": d, "pnl_twd": round(pnl, 0), "pnl_pct": pct(pnl)})
    # ★ 2026-11-13（外部審查：stop-risk 上限不等於尾端風險）連續跌停情境：漲跌停鎖死時停損單賣不掉，
    # 損失不受停損價保護。假設多單連續 k 天鎖跌停（每天 −10%，複利）、空單連續 k 天鎖漲停（每天 +10%）。
    # 「全部部位」與「只有最大產業」兩種。純試算，不影響任何發訊或風控判斷。
    def _lock(vals_long, vals_short, k):
        return -(vals_long * (1 - 0.9 ** k)) - (vals_short * (1.1 ** k - 1))
    top_sec = sectors[0]["sector"] if sectors else None
    sec_long = sum(x["value"] for x in rows if x["sector"] == top_sec and x["direction"] == "buy")
    sec_short = sum(x["value"] for x in rows if x["sector"] == top_sec and x["direction"] == "sell")
    gap_scenarios = []
    for k in (1, 2, 3):
        a_ = _lock(long_v, short_v, k); s_ = _lock(sec_long, sec_short, k)
        gap_scenarios.append({"limit_days": k, "all_pnl_twd": round(a_, 0), "all_pnl_pct": pct(a_),
                              "top_sector": top_sec, "top_sector_pnl_twd": round(s_, 0), "top_sector_pnl_pct": pct(s_),
                              "vs_stop_loss_x": round(abs(a_) / worst, 2) if worst else None})
    hard_port = bal * MAX_PORTFOLIO_RISK
    single_cap = bal / max(1, MAX_SIMULTANEOUS_POSITIONS)
    flags = []
    if worst > hard_port:
        flags.append(f"🔴 最壞同時停損 {pct(worst)}% 超過組合硬上限 {round(MAX_PORTFOLIO_RISK*100)}%，已暫停新增訊號")
    elif worst > bal * MAX_DAILY_RISK:
        flags.append(f"⚠️ 最壞同時停損 {pct(worst)}%，高於每日風險上限 {round(MAX_DAILY_RISK*100,1)}%（接近組合硬上限 {round(MAX_PORTFOLIO_RISK*100)}%）")
    if largest["value"] > single_cap:
        flags.append(f"⚠️ 單一部位 {largest.get('code')} 佔 {pct(largest['value'])}%，超過單檔上限 {pct(single_cap)}%")
    if n > MAX_SIMULTANEOUS_POSITIONS:
        flags.append(f"⚠️ 持倉 {n} 檔，超過同時上限 {MAX_SIMULTANEOUS_POSITIONS}")
    if sectors and sectors[0]["pct"] >= 50:
        flags.append(f"⚠️ 產業集中：{sectors[0]['sector']} 佔 {sectors[0]['pct']}%")
    return {"ok": True, "n": n, "balance": round(bal, 0),
            "gross_exposure": round(gross, 0), "gross_pct": pct(gross),
            "long_pct": pct(long_v), "short_pct": pct(short_v), "net_pct": pct(long_v - short_v),
            "worst_case_loss": round(worst, 0), "worst_case_loss_pct": pct(worst),
            "max_daily_risk_pct": round(MAX_DAILY_RISK * 100, 1),
            "max_portfolio_risk_pct": round(MAX_PORTFOLIO_RISK * 100, 1),   # ★ LIVE03：組合硬上限（前端組合風險面板用）
            "sectors": sectors[:8],
            "largest": {"code": largest.get("code"), "name": largest.get("name"),
                        "value": round(largest["value"], 0), "pct": pct(largest["value"])},
            "scenarios": scenarios, "gap_scenarios": gap_scenarios, "flags": flags,
            "gap_basis": "連續跌停情境：多單每天 −10%（空單每天 +10%）且無法成交，連續 1／2／3 天；vs_stop_loss_x＝"
                         "該情境損失是「全部同時停損」的幾倍。停損風險上限管不到這種情況。",
            "loss_basis": "最壞同時停損＝各部位（進場價−停損價）×股數之總和，未含手續費/稅與跳空；"
                          "實際遇開盤跳空或流動性不足時損失可能超過此數，非實損上限。",
            "note": "情境為 beta=1 簡化假設（部位隨大盤等比變動），非精確個股 beta；僅供組合風險概覽，不構成投資建議。"}


def high_conf_ok(sig: Dict, record: Dict) -> bool:
    """這一檔訊號是否符合「持倉滿額也可通知」的例外條件。"""
    if not record.get("perfect"):
        return False
    try:
        return float(sig.get("score") or 0) >= HC_MIN_SCORE and sig.get("direction") in ("buy", "sell")
    except Exception:
        return False
