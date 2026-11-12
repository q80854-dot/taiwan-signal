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

SIM_VERSION = "exit-v3-2026-11-12"            # v3：漲跌停鎖死日無法出場則順延（Q-P0-07）；v2：到期窗截斷
STAGED_SIM_VERSION = "staged-v3-2026-11-12"   # v3：同上；v2：加 expire_days 到期窗截斷（Phase B）
_TPE = timezone(timedelta(hours=8))

def bar_ok(b) -> bool:
    """這根 K 棒的最高、最低、收盤是否為有效正數（非 None、非 NaN）。
    ★ 2026-11-12（由線上清洗日誌揭露）：資料源偶有整根為 NaN 的日 K（停牌日或抓取異常）。
    NaN 與任何數比較皆為 False——出場條件永不成立、MFE/MAE 停在初始哨兵值 ±9e9 而寫進資料庫
    （線上實例：3714.TW 2026-11-04 的假想單 mae_r=9e9），分批模擬則會以 NaN 收盤結算出 NaN 損益。
    無效 K 棒一律跳過（視為當日無資料），不參與任何判定。"""
    try:
        for k in ("high", "low", "close"):
            v = b.get(k)
            if v is None or v != v or v <= 0:
                return False
        return True
    except Exception:
        return False


LIMIT_LOCK_PCT = 9.5     # 台股漲跌幅限制 10%；因升降單位取整，鎖死價相對前收的實際幅度略小於 10%


def limit_locked(bar, prev_close):
    """★ 2026-11-12（Q-P0-07 可成交性）：這根日 K 是否「一價到底鎖在漲停或跌停」。
    回傳 "up"／"down"／None。判定只用既有日 K（不需額外資料）：整天最高＝最低（沒有任何價格區間）
    且相對前一日收盤漲跌幅 ≥ 9.5%。鎖漲停＝買不到（賣得掉）；鎖跌停＝賣不掉（買得到）。
    盤中曾打開過的漲跌停（最高≠最低）仍有成交機會，不算鎖死——只擋「確定無法成交」的情形，
    不做樂觀或悲觀的猜測。缺前收或資料不全回 None（不判定）。"""
    try:
        hi, lo, cl = bar.get("high"), bar.get("low"), bar.get("close")
        if not prev_close or prev_close <= 0 or hi is None or lo is None or cl is None:
            return None
        if abs(hi - lo) > 1e-9:
            return None
        chg = (cl / prev_close - 1) * 100
        if chg >= LIMIT_LOCK_PCT:
            return "up"
        if chg <= -LIMIT_LOCK_PCT:
            return "down"
    except Exception:
        return None
    return None


def entry_blocked_by_limit(direction, bar, prev_close):
    """進場當根是否因鎖死而確定無法成交：做多遇鎖漲停（買不到）、放空遇鎖跌停（賣不出）。"""
    lk = limit_locked(bar, prev_close)
    return (direction == "buy" and lk == "up") or (direction == "sell" and lk == "down")


def exit_blocked_by_limit(direction, bar, prev_close):
    """出場當根是否因鎖死而確定無法成交：多單遇鎖跌停（賣不掉）、空單遇鎖漲停（補不回）。"""
    lk = limit_locked(bar, prev_close)
    return (direction == "buy" and lk == "down") or (direction == "sell" and lk == "up")


def simulate_exit(planned_entry, entry_fill, stop, direction, bars_after, bar_date,
                  expire_days, risk, tp1=None, tp2=None, tp3=None, now=None, prev_close=None):
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
      prev_close     訊號日收盤（判定第一根是否鎖死用；之後各根以前一根收盤判定）。缺則第一根不判定。
    v3：出場當根若一價鎖死在不利方向（多單鎖跌停／空單鎖漲停），確定無法成交，該根不出場、順延。
    """
    buy = direction == "buy"
    sgn = 1 if buy else -1
    out = {"result": None, "exit_price": None, "exit_date": None, "bars_held": len(bars_after),
           "mfe": None, "mae": None, "gap": None, "r1": None, "r5": None, "r10": None, "r20": None,
           "mae_bars": None, "mfe_bars": None, "tp1_before_sl": None,
           "entry_fill": entry_fill, "sim_version": SIM_VERSION}
    bars_after = [b for b in (bars_after or []) if bar_ok(b)]      # 無效（NaN／缺值）K 棒視為當日無資料
    out["bars_held"] = len(bars_after)
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
    _pc = prev_close
    for i, b in enumerate(after):
        hi, lo, op = b["high"], b["low"], b.get("open")
        _locked = exit_blocked_by_limit(direction, b, _pc)
        _pc = b.get("close") or _pc
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
        if _locked:
            continue                                  # 鎖死無法出場：MAE/MFE 照記，出場順延到可成交的 K 棒
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
    # 哨兵值不得外洩：沒有任何有效更新就回 None（不寫入 ±9e9）
    out["mfe"] = round(mfe, 3) if mfe > -1e9 else None
    out["mae"] = round(mae, 3) if mae < 1e9 else None
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

    def __init__(self, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn, expire_days=None,
                 prev_close=None):
        self.prev_close = prev_close          # 判定漲跌停鎖死用：初值＝訊號日收盤，之後逐棒更新
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
        if not bar_ok(b):
            return []                  # 無效（NaN／缺值）K 棒：當日視為無資料，不成交、不更新前收
        # v3（Q-P0-07）：一價鎖死在不利方向（多單鎖跌停／空單鎖漲停）＝確定賣不掉／補不回，
        # 這根不產生任何成交，停損與到期都順延到下一根可成交的 K 棒。
        locked = exit_blocked_by_limit(self.direction, b, self.prev_close)
        self.prev_close = b.get("close") or self.prev_close
        if locked:
            self.locked_bars = getattr(self, "locked_bars", 0) + 1
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
        if self.remaining <= 0 or not bar_ok(b):
            return None
        return self._leg(stage, b["close"], self.remaining, b["bar_date"])

    def result(self):
        tp_hits = [l["stage"] for l in self.legs if l["stage"] in ("tp1", "tp2", "tp3")]
        return tp_hits[-1] if tp_hits else ("sl" if any(l["stage"] == "stop" for l in self.legs) else "expired")


def simulate_staged_exit(bars, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn,
                         expire_days=None, prev_close=None):
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
    pos = StagedPosition(entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn, expire_days,
                         prev_close=prev_close)
    for b in bars:
        if pos.remaining <= 0:
            break
        pos.on_bar(b)
    if pos.remaining > 0 and bars:
        _valid = [b for b in bars if bar_ok(b)]
        if _valid:
            pos.force_close(_valid[-1])     # 以最後一根「有效」K 棒收盤暫估，不用 NaN
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


# ───────────── 出場 Challenger（只供影子追蹤與研究；正式訊號不使用） ─────────────
# 2026-11-12 出場方式研究（docs/research/EXIT_STUDY_2020_2026.md）：把正式訊號重播 2020–2026，
# 對同一批訊號逐筆比較 8 個事前寫死的出場方案。延長持有與移動停損在訓練期、驗證期的平均淨 R 都
# 高於現行出場，但移動停損的優勢幾乎全來自極少數大漲的單（縮尾後優勢消失）。因此不改正式出場，
# 先把下列兩個方案掛進影子追蹤平行記錄，以登錄日之後的真實資料驗證（實驗 E07／E08）。
ALT_EXIT_SIM_VERSION = "alt-exit-v2-2026-11-12"   # v2：新增 exp15；與該列 Champion 相同的方案不重複計算
EXIT_CHALLENGERS = {
    # 2026-11-12 使用者核准把正式到期改為 60 天。之後產生的列，Champion 本身就是 60 天，改以
    # 「原本的 15 天到期」當對照（exp15），持續確認延長到期是否真的較好；之前產生的列（Champion＝15 天）
    # 則以 hold60 當對照。與該列自己的 Champion 出場相同的方案不重複計算（見 shadow._alt_exit_tracks）。
    "exp15": {"name": "分批停利不變、到期 15 天（2026-11-12 以前的正式出場）", "expire": 15, "ladder": True},
    # 分批停利不變，到期 60 個日曆天（研究的 V3；2026-11-12 起即正式出場）
    "hold60": {"name": "分批停利不變、到期 60 天", "expire": 60, "ladder": True},
    # 不分批、不設停利：全程以「進場後最高（低）價 ∓ 3 ATR」移動停損，最長 60 天（研究的 V5）
    "trail3": {"name": "不分批：全程 3 ATR 移動停損、最長 60 天", "expire": 60, "ladder": False,
               "trail_atr": 3.0, "trail_after": "entry"},
}


class FlexStagedPosition(StagedPosition):
    """在正式分批狀態機之上加『移動停損』與『時間停損』，其餘成交慣例完全沿用父類別（同一根 K 棒
    停損優先、跳空以開盤價成交、一價鎖死順延、無效 K 棒略過）。移動停損以「到本根為止」的極值
    更新、下一根才生效——沒有未來函數；只會收緊，不會放寬原始停損。"""

    def configure(self, trail_atr=None, atr=None, trail_after="entry", time_stop=None):
        self.trail_atr, self.atr, self.trail_after, self.time_stop = trail_atr, atr, trail_after, time_stop
        self.extreme = None
        return self

    def on_bar(self, b):
        # 一價鎖死（賣不掉／補不回）要在呼叫父類別之前判定——父類別會更新 prev_close
        locked = bar_ok(b) and exit_blocked_by_limit(self.direction, b, self.prev_close)
        legs = super().on_bar(b)
        if self.remaining <= 0 or not bar_ok(b) or locked:
            return legs
        bd = b["bar_date"]
        # 時間停損：到指定日曆天仍未成交任何一段停利 → 以本根收盤出場
        if self.time_stop and not self.done["tp1"] and self.days(bd) >= self.time_stop:
            leg = self.force_close(b, stage="time_stop")
            return legs + ([leg] if leg else [])
        if self.trail_atr and self.atr and (self.trail_after == "entry" or self.done["tp1"]):
            if self.buy:
                self.extreme = b["high"] if self.extreme is None else max(self.extreme, b["high"])
                self.current_sl = max(self.current_sl, self.extreme - self.trail_atr * self.atr)
            else:
                self.extreme = b["low"] if self.extreme is None else min(self.extreme, b["low"])
                self.current_sl = min(self.current_sl, self.extreme + self.trail_atr * self.atr)
        return legs


def simulate_alt_exit(spec, bars, entry, sl, tp1, tp2, tp3, direction, shares, gen_date, pnl_fn,
                      atr=None, prev_close=None):
    """以某個出場方案（spec：expire／ladder／drop_tp3／trail_atr／trail_after／time_stop）模擬。
    回傳 {pnl, exit_date, last, open_end}；open_end=True 代表資料用盡時仍有部位在場（不補暫估腿，
    由呼叫端決定如何標示）。需要 ATR 的方案在 atr 缺漏時回 None（不猜）。"""
    if spec.get("trail_atr") and not atr:
        return None
    ladder = spec.get("ladder", True)
    pos = FlexStagedPosition(entry, sl, tp1 if ladder else None, tp2 if ladder else None,
                             tp3 if (ladder and not spec.get("drop_tp3")) else None,
                             direction, shares, gen_date, pnl_fn, spec.get("expire"), prev_close=prev_close)
    pos.configure(spec.get("trail_atr"), atr, spec.get("trail_after", "entry"), spec.get("time_stop"))
    for b in bars:
        if pos.remaining <= 0:
            break
        pos.on_bar(b)
    if pos.remaining > 0:
        return {"open_end": True, "pnl": None, "exit_date": pos.exit_date, "last": None}
    if not pos.legs:
        return None
    return {"open_end": False, "pnl": sum(l["pnl"] for l in pos.legs), "exit_date": pos.exit_date,
            "last": pos.legs[-1]["stage"]}
