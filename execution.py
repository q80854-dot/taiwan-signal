"""單一權威出場模擬器（2026-11-06，Phase 1：統一回測/shadow 成交模擬）。

目標：讓 shadow 追蹤、回測、paper 以後都共用「同一套出場判定」，相同輸入必得相同結果，
避免各自實作漂移。本模組只負責「進場後，依後續 K 棒判定出場」這段共用核心，不處理
資料讀取、部位大小、費稅（那些由各呼叫端負責）。

保守成交規則（與實盤 scanner._resolve_pending_signals、shadow._simulate 一致）：
  · 只看訊號日之後的 K 棒（由呼叫端過濾後傳入）。
  · 同一根 K 同時觸及停損與停利 → 先判停損（保守，不取較樂觀的停利）。
  · 跳空穿越停損 → 以開盤價成交（不假設能在停損價剛好成交）。
  · 停利優先序 tp3 > tp2 > tp1。
  · 逾期（日曆天 ≥ expire_days）仍未出場 → 以最新收盤結算為 expired。

R 一律以傳入的 risk（＝|規劃進場價−停損|）為分母，跨筆可比；分子以實際進場價計算。

版本：SIM_VERSION 供報告標註「用哪一版模擬器」，之後規則若調整務必 bump。
"""
from datetime import datetime, timezone, timedelta

SIM_VERSION = "exit-v1-2026-11-06"
STAGED_SIM_VERSION = "staged-v1-2026-11-08"
_TPE = timezone(timedelta(hours=8))


def simulate_exit(planned_entry, entry_fill, stop, direction, bars_after, bar_date,
                  expire_days, risk, tp1=None, tp2=None, tp3=None, now=None):
    """依後續 K 棒判定出場，回傳與 shadow._simulate 相同結構的 dict。

    參數：
      planned_entry  訊號規劃進場價（算跳空 gap 用）。
      entry_fill     實際進場價（通常為訊號日後第一根開盤）。
      stop           停損價（規劃值，不變）。
      direction      "buy" / "sell"。
      bars_after     訊號日之後、由舊到新的 K 棒 list（每個含 open/high/low/close/bar_date）。
      bar_date       訊號日（算逾期用）。
      expire_days    逾期日曆天數。
      risk           1R 的分母（>0；呼叫端須先確保非退化）。
      tp1/tp2/tp3    停利價（可缺）。
      now            覆寫「現在時間」（測試用）；預設台北現在。
    """
    buy = direction == "buy"
    sgn = 1 if buy else -1
    out = {"result": None, "exit_price": None, "exit_date": None, "bars_held": len(bars_after),
           "mfe": None, "mae": None, "gap": None, "r1": None, "r5": None, "r10": None, "r20": None,
           "mae_bars": None, "mfe_bars": None, "tp1_before_sl": None,
           "entry_fill": entry_fill, "sim_version": SIM_VERSION}
    if not bars_after or not risk:
        return out
    after = bars_after
    out["gap"] = round((after[0]["open"] / planned_entry - 1) * 100, 2) if after[0].get("open") else None
    out["r1"] = round(sgn * (after[0]["close"] - entry_fill) / risk, 3)
    if len(after) >= 5:
        out["r5"] = round(sgn * (after[4]["close"] - entry_fill) / risk, 3)
    if len(after) >= 10:
        out["r10"] = round(sgn * (after[9]["close"] - entry_fill) / risk, 3)
    if len(after) >= 20:
        out["r20"] = round(sgn * (after[19]["close"] - entry_fill) / risk, 3)
    mfe, mae = -9e9, 9e9
    mfe_i = mae_i = tp1_i = sl_i = None
    for i, b in enumerate(after):
        hi, lo, op = b["high"], b["low"], b.get("open")
        fav = sgn * ((hi if buy else lo) - entry_fill) / risk
        adv = sgn * ((lo if buy else hi) - entry_fill) / risk
        if fav > mfe:
            mfe, mfe_i = fav, i + 1
        if adv < mae:
            mae, mae_i = adv, i + 1
        sl_hit = lo <= stop if buy else hi >= stop
        h3 = bool(tp3) and ((hi >= tp3) if buy else (lo <= tp3))
        h2 = bool(tp2) and ((hi >= tp2) if buy else (lo <= tp2))
        h1 = bool(tp1) and ((hi >= tp1) if buy else (lo <= tp1))
        if h1 and tp1_i is None:
            tp1_i = i + 1
        if sl_hit and sl_i is None:
            sl_i = i + 1
        if not (sl_hit or h1 or h2 or h3):
            continue
        if sl_hit:                                    # 同根同時觸及 → 先判停損（保守）
            res, px = "sl", stop
            if op and ((buy and op < stop) or (not buy and op > stop)):
                px = op                               # 跳空穿越停損 → 開盤成交
        elif h3:
            res, px = "tp3", tp3
        elif h2:
            res, px = "tp2", tp2
        else:
            res, px = "tp1", tp1
        out.update(result=res, exit_price=px, exit_date=b["bar_date"][:10], bars_held=i + 1)
        break
    out["mfe"], out["mae"] = round(mfe, 3), round(mae, 3)
    out["mfe_bars"], out["mae_bars"] = mfe_i, mae_i
    if tp1_i is not None:
        out["tp1_before_sl"] = int(sl_i is None or tp1_i <= sl_i)
    elif sl_i is not None:
        out["tp1_before_sl"] = 0
    if out["result"] is None:                         # 逾期以最新收盤結算
        try:
            ref = now or datetime.now(_TPE).replace(tzinfo=None)
            age = (ref - datetime.strptime(bar_date, "%Y-%m-%d")).days
        except Exception:
            age = 0
        if age >= expire_days:
            out.update(result="expired", exit_price=after[-1]["close"], exit_date=after[-1]["bar_date"][:10])
    return out


def simulate_staged_exit(bars, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn):
    """分批出場模擬器（2026-11-08，Phase 1：backtester 與此共用同一實作）。
    忠實模擬推播給使用者的操作計畫：TP1/TP2/TP3 各出 1/3；TP1 觸及後停損移至成本、
    TP2 觸及後移至 TP1；同一根 K 棒停損優先於停利（保守）；跳空穿越停損以開盤價成交；
    末段未出場者到最後一根以收盤價結算（expired）。

    本模組不綁定特定成本模型：每段損益由呼叫端傳入的 pnl_fn 計算，簽名需為
    pnl_fn(entry, exit_price, direction, shares, days_held=int) -> number（含滑價/費稅/借券）。
    這樣 backtester 可沿用自己的 calc_tw_pnl、數字完全不變，仍收斂到單一出場實作。

    bars：訊號日之後、時間升冪的日K清單，每筆含 bar_date/open/high/low/close。
    回傳 {result, exit_price, pnl, pnl_pct, legs, exit_date, sim_version}；result 取「觸及的最高
    停利」以相容既有勝率定義（tp*/sl/expired），pnl 為各段加總的真實損益。
    """
    buy = direction == "buy"
    s1 = shares // 3; s2 = shares // 3; s3 = shares - s1 - s2
    leg_sh = {"tp1": s1, "tp2": s2, "tp3": s3}
    tgt = {"tp1": tp1, "tp2": tp2, "tp3": tp3}
    done = {"tp1": False, "tp2": False, "tp3": False}
    remaining = shares; current_sl = sl; legs = []; exit_date = None

    def _days(bd):
        try:
            return max(0, (datetime.strptime(bd[:10], "%Y-%m-%d") - datetime.strptime(gen_date[:10], "%Y-%m-%d")).days)
        except Exception:
            return 0

    for b in bars:
        if remaining <= 0:
            break
        op, hi, lo, bd = b.get("open"), b["high"], b["low"], b["bar_date"]
        hit_stop = (lo <= current_sl) if buy else (hi >= current_sl)
        if hit_stop:
            px = current_sl
            if op and ((buy and op < current_sl) or (not buy and op > current_sl)):
                px = op                      # 跳空穿越停損：以較差的開盤價成交
            legs.append({"stage": "stop", "price": px, "shares": remaining,
                         "pnl": pnl_fn(entry, px, direction, remaining, days_held=_days(bd))})
            exit_date = bd[:10]; remaining = 0
            break
        for stage in ("tp1", "tp2", "tp3"):
            if done[stage] or leg_sh[stage] <= 0:
                continue
            t = tgt[stage]
            if not t:
                continue
            hit = (hi >= t) if buy else (lo <= t)
            if not hit:
                break                        # 較近的目標沒碰到，當天更遠的目標不可能碰到
            legs.append({"stage": stage, "price": t, "shares": leg_sh[stage],
                         "pnl": pnl_fn(entry, t, direction, leg_sh[stage], days_held=_days(bd))})
            done[stage] = True; remaining -= leg_sh[stage]; exit_date = bd[:10]
            if stage == "tp1":   current_sl = entry        # 移至保本
            elif stage == "tp2": current_sl = tp1          # 移至 TP1 鎖利
    if remaining > 0 and bars:
        last = bars[-1]
        legs.append({"stage": "expired", "price": last["close"], "shares": remaining,
                     "pnl": pnl_fn(entry, last["close"], direction, remaining, days_held=_days(last["bar_date"]))})
        exit_date = last["bar_date"][:10]; remaining = 0
    if not legs:
        return None
    total_pnl = round(sum(l["pnl"] for l in legs), 0)
    tp_hits = [l["stage"] for l in legs if l["stage"] in ("tp1", "tp2", "tp3")]
    result = tp_hits[-1] if tp_hits else ("sl" if any(l["stage"] == "stop" for l in legs) else "expired")
    sh = sum(l["shares"] for l in legs)
    exit_price = round(sum(l["price"] * l["shares"] for l in legs) / sh, 2) if sh else entry
    pnl_pct = round(total_pnl / (entry * shares) * 100, 2) if entry and shares else 0
    return {"result": result, "exit_price": exit_price, "pnl": total_pnl, "pnl_pct": pnl_pct,
            "legs": legs, "exit_date": exit_date, "sim_version": STAGED_SIM_VERSION}
