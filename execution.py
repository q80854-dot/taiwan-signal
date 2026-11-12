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

SIM_VERSION = "exit-v2-2026-11-09"            # v2：加到期窗截斷＋同根 K tp1_before_sl 保守化（Phase B）
STAGED_SIM_VERSION = "staged-v2-2026-11-09"   # v2：加 expire_days 到期窗截斷（Phase B）
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
    try:
        _sig_dt = datetime.strptime(bar_date[:10], "%Y-%m-%d")
    except Exception:
        _sig_dt = None
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
            # ★ 2026-11-09 Phase B：到期窗截斷（消除 look-ahead）。本根未觸及任何出場，
            # 若其日曆天齡已達 expire_days，則以本根收盤結算 expired 並停止往後掃描——
            # 否則到期日之後才觸及的停利/停損會被誤計入出場（高估勝率）。
            if _sig_dt is not None and expire_days:
                try:
                    _age_b = (datetime.strptime(b["bar_date"][:10], "%Y-%m-%d") - _sig_dt).days
                except Exception:
                    _age_b = -1
                if _age_b >= expire_days:
                    out.update(result="expired", exit_price=b["close"],
                               exit_date=b["bar_date"][:10], bars_held=i + 1)
                    break
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
        # ★ 2026-11-09 Phase B：同一根 K 棒同時觸及 TP1 與 SL（tp1_i == sl_i）時，無法得知盤中
        # 先後，依整體「同根先判停損」的保守慣例視為 SL 先（tp1_before_sl=0）。故用嚴格 < 。
        out["tp1_before_sl"] = int(sl_i is None or tp1_i < sl_i)
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


def staged_open_at_end(sim, gen_date, expire_days) -> bool:
    """★ 2026-11-12（S-P0-01／S-P1-02）：分批模擬結束時是否「其實還有部位在場」。
    simulate_staged_exit 對沒出完的部位，一律以最後一根 K 棒收盤補一腿 expired。那一腿只有在
    該 K 棒的日曆天齡已達 expire_days 時才是真正的到期結算；否則只是「以最新收盤暫估」，
    呼叫端不得把它當定案值。未設 expire_days 時無到期概念，補的那一腿一律視為暫估。"""
    legs = (sim or {}).get("legs") or []
    if not legs or legs[-1].get("stage") != "expired":
        return False
    if not expire_days:
        return True
    try:
        age = (datetime.strptime(str(sim.get("exit_date"))[:10], "%Y-%m-%d")
               - datetime.strptime(str(gen_date)[:10], "%Y-%m-%d")).days
    except Exception:
        return True
    return age < expire_days


class StagedPosition:
    """★ 2026-11-12（Q-P0-01）分批出場的『逐棒狀態機』。simulate_staged_exit 與組合級回測共用，
    兩者的出場規則因此不可能漂移：TP1/TP2/TP3 各出 1/3；TP1 觸及後停損移至成本、TP2 觸及後
    移至 TP1；同一根 K 棒停損優先於停利（保守）；跳空穿越停損以開盤價成交；到期窗以該根收盤
    結算剩餘。每餵一根 K 棒回傳這根新成交的腿，呼叫端可即時更新資金、風險與權益曲線。"""

    def __init__(self, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn, expire_days=None):
        self.entry, self.direction, self.shares = entry, direction, shares
        self.buy = direction == "buy"
        self.gen_date, self.pnl_fn, self.expire_days = gen_date, pnl_fn, expire_days
        s1 = shares // 3; s2 = shares // 3; s3 = shares - s1 - s2
        self.leg_sh = {"tp1": s1, "tp2": s2, "tp3": s3}
        self.tgt = {"tp1": tp1, "tp2": tp2, "tp3": tp3}
        self.done = {"tp1": False, "tp2": False, "tp3": False}
        self.tp1 = tp1
        self.remaining = shares
        self.current_sl = sl
        self.legs = []
        self.exit_date = None

    def days(self, bd):
        try:
            return max(0, (datetime.strptime(bd[:10], "%Y-%m-%d")
                           - datetime.strptime(self.gen_date[:10], "%Y-%m-%d")).days)
        except Exception:
            return 0

    def _leg(self, stage, price, shares, bd):
        leg = {"stage": stage, "price": price, "shares": shares,
               "pnl": self.pnl_fn(self.entry, price, self.direction, shares, days_held=self.days(bd))}
        self.legs.append(leg)
        self.remaining -= shares
        self.exit_date = bd[:10]
        return leg

    def on_bar(self, b):
        """餵入一根 K 棒，回傳這根新成交的腿（可能為空）。部位已出清時不做事。"""
        if self.remaining <= 0:
            return []
        new = []
        buy = self.buy
        op, hi, lo, bd = b.get("open"), b["high"], b["low"], b["bar_date"]
        hit_stop = (lo <= self.current_sl) if buy else (hi >= self.current_sl)
        if hit_stop:
            px = self.current_sl
            if op and ((buy and op < self.current_sl) or (not buy and op > self.current_sl)):
                px = op                      # 跳空穿越停損：以較差的開盤價成交
            new.append(self._leg("stop", px, self.remaining, bd))
            return new
        for stage in ("tp1", "tp2", "tp3"):
            if self.done[stage] or self.leg_sh[stage] <= 0:
                continue
            t = self.tgt[stage]
            if not t:
                continue
            hit = (hi >= t) if buy else (lo <= t)
            if not hit:
                break                        # 較近的目標沒碰到，當天更遠的目標不可能碰到
            new.append(self._leg(stage, t, self.leg_sh[stage], bd))
            self.done[stage] = True
            if stage == "tp1":   self.current_sl = self.entry      # 移至保本
            elif stage == "tp2": self.current_sl = self.tp1        # 移至 TP1 鎖利
        # ★ 2026-11-09 Phase B：到期窗截斷。本根收盤時若已達 expire_days 且仍有未出場部位，
        # 以本根收盤結算剩餘為 expired，停止往後掃描——否則到期日之後才觸及的停利會被誤計入
        # 出場（高估勝率/報酬）。與 simulate_exit 的到期語意一致。
        if self.expire_days and self.remaining > 0 and self.days(bd) >= self.expire_days:
            new.append(self._leg("expired", b["close"], self.remaining, bd))
        return new

    def force_close(self, b, stage="expired"):
        """以指定 K 棒收盤結算剩餘部位（資料用盡時的暫估／回測期末平倉）。"""
        if self.remaining <= 0:
            return None
        return self._leg(stage, b["close"], self.remaining, b["bar_date"])

    def result(self):
        tp_hits = [l["stage"] for l in self.legs if l["stage"] in ("tp1", "tp2", "tp3")]
        return tp_hits[-1] if tp_hits else ("sl" if any(l["stage"] == "stop" for l in self.legs) else "expired")


def simulate_staged_exit(bars, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn,
                         expire_days=None):
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

    ★ 2026-11-12：內部改由 StagedPosition 逐棒狀態機執行（行為與輸出完全不變，由既有特徵化
    測試鎖定）；組合級回測直接使用同一個狀態機。
    """
    pos = StagedPosition(entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn, expire_days)
    for b in bars:
        if pos.remaining <= 0:
            break
        pos.on_bar(b)
    if pos.remaining > 0 and bars:
        pos.force_close(bars[-1])
    legs, exit_date = pos.legs, pos.exit_date
    if not legs:
        return None
    total_pnl = round(sum(l["pnl"] for l in legs), 0)
    result = pos.result()
    sh = sum(l["shares"] for l in legs)
    exit_price = round(sum(l["price"] * l["shares"] for l in legs) / sh, 2) if sh else entry
    pnl_pct = round(total_pnl / (entry * shares) * 100, 2) if entry and shares else 0
    return {"result": result, "exit_price": exit_price, "pnl": total_pnl, "pnl_pct": pnl_pct,
            "legs": legs, "exit_date": exit_date, "sim_version": STAGED_SIM_VERSION}
