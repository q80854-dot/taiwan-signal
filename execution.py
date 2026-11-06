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
