"""Champion 歷史訊號重播（研究工具；不被 app 匯入、不影響任何線上行為）。

目的：回答「正式策略到底有沒有優勢、輸在哪裡」。影子追蹤只累積了約一個月，樣本集中在同一段
行情；本工具把**正式訊號引擎原封不動**（signal_engine.generate_signal_tw：評分、放空關卡、
大盤熔斷、追價濾網、停損停利、部位）逐日套在多年歷史上，每個訊號再用**與正式推播同一個
分批出場狀態機**（execution.StagedPosition）與影子同口徑的成本算出 Champion 淨 R。

時序（無未來函數）：訊號日 s 只用到 s（含）為止的日 K、以及「已經走完」的週 K；隔日開盤成交；
停損停利以訊號日收盤定錨；15 個日曆天到期；一價鎖死的漲跌停進場不成交、出場順延。

與線上的已知差異（都會寫進輸出，解讀時必須一併看）：
  · 沒有歷史小時線 → 沒有「小時線共振 +10 分」，達標訊號會比線上少、分數偏低。
  · 沒有歷史的三大法人、籌碼、月營收、重大訊息、除權息資料 → 這些加減分與擋單不套用。
  · 市場情緒分數無歷史值，一律以 50（中性）代入；外資賣超熔斷不套用；VIX 以 20 代入。
  · 規模分類（決定停損倍數與停利盈虧比）以參數指定，非歷史當時。
  · 同一檔在前一筆訊號出場前不重複進場（線上同樣排除已有未平倉訊號的標的）；
    但不模擬「同時最多 5 檔」與每日訊號上限——這裡量的是『每一筆候選訊號』的期望值，
    與影子追蹤的 candidate 同一個概念，不是帳戶淨值。

用法：python research/champion_replay.py --start 2021-01-01 --end 2026-11-12 --out replay.csv
"""
import argparse
import bisect
import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from multiprocessing import Pool

os.environ.setdefault("ACCOUNT_BALANCE_TWD", "1000000")      # 與線上本金一致（影響放空整張規則）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)                              # 逐日呼叫引擎會產生大量一般日誌

DAILY_WINDOW = 250      # 線上日線為近 1 年
WEEKLY_WINDOW = 260     # 線上週線為近 5 年
FIELDS = ["ticker", "signal_date", "size_cat", "direction", "score", "grade", "adx", "rsi", "vol_ratio", "weekly_bias",
          "chase_level", "ext_atr", "chg_pct", "sl_pct", "sl_atr", "rr1", "min_score_used", "regime", "twii_chg",
          "entry_plan", "stop", "tp1", "tp2", "tp3", "fill_date", "fill", "gap_pct", "status", "result",
          "legs", "exit_date", "hold_days", "champion_r", "mfe_r", "mae_r", "tp1_before_sl",
          # 不設停損停利的原始前瞻報酬（%，自隔日開盤起算；方向已調整：放空為反向）與同期加權指數報酬
          "fwd5_pct", "fwd10_pct", "fwd20_pct", "mkt5_pct", "mkt10_pct", "mkt20_pct", "cost_r"]


def _market_maps(start):
    """加權指數逐日：單日漲跌幅、中期趨勢（與 data_fetcher._regime_from_closes 同一函式）。"""
    import backtester as bt
    from data_fetcher import _regime_from_closes
    d = bt._fetch_long_history_tw("^TWII", "daily", "max")
    if not d:
        raise SystemExit("抓不到 ^TWII 歷史資料，無法重播（不以假資料代替）")
    out = {}
    cl, ds = d["closes"], d["dates"]
    for i in range(65, len(cl)):
        if ds[i] < start:
            continue
        reg = _regime_from_closes(cl[max(0, i - 199):i + 1], "replay") or {"available": False, "trend": "unknown"}
        fw = {}
        for k in (5, 10, 20):                      # 指數自隔日開盤（以隔日開盤近似為 i+1 的開盤）到第 k 根收盤
            if i + k < len(cl) and d["opens"][i + 1]:
                fw[k] = (cl[i + k] / d["opens"][i + 1] - 1) * 100
        out[ds[i]] = {"chg": (cl[i] / cl[i - 1] - 1) * 100 if cl[i - 1] else 0.0, "regime": reg, "fwd": fw}
    return out


def _overview(m):
    from config import CIRCUIT_BREAKER as CB
    chg = m["chg"]
    status = "stop" if chg <= CB["twii_drop_stop"] else "caution" if chg <= CB["twii_drop_caution"] else "normal"
    return {"can_trade": status != "stop", "market_status": status, "sentiment_score": 50, "regime": m["regime"],
            "index": {"twii": {"chg": chg, "source": "replay"}, "vix": {"price": 20, "source": "replay"}},
            "foreign": {"net_buy_twd": 0}, "margin": {"chg_pct": 0}}, status


def replay_ticker(args):
    ticker, start, end, size_cat, pit, mkt = args
    import backtester as bt
    import config
    import event_filter
    import execution
    import shadow
    import signal_engine as se
    event_filter.check = lambda code, within_days=2: {}        # 無歷史除權息／重大訊息資料：不套用（已於說明揭露）
    rows = []
    try:
        d = bt._fetch_long_history_tw(ticker, "daily", "max")
        w = bt._fetch_long_history_tw(ticker, "weekly", "max")
    except Exception:
        d = w = None
    if not d or len(d.get("closes") or []) < DAILY_WINDOW + 5:
        return ticker, rows, "no_data"
    ds, op, hi, lo, cl, vo = d["dates"], d["opens"], d["highs"], d["lows"], d["closes"], d["volumes"]
    wd = (w or {}).get("dates") or []
    code = ticker.split(".")[0]
    info = {"name": code, "code": code, "sector": "", "size_cat": size_cat, "volume_lots": None}
    base_min = config.SIGNAL_THRESHOLDS["min_score"]
    expire = config.CIRCUIT_BREAKER.get("signal_expire_days", 15)
    pnl_fn = shadow._shadow_staged_pnl_factory()
    if pit == "tw50":
        import tw50_history as h
    import stock_universe as su
    min_vol = config.SIGNAL_THRESHOLDS.get("min_avg_volume", 0)
    busy_until = ""
    for s in range(DAILY_WINDOW, len(cl) - 1):
        day = ds[s]
        if day < start or day > end or day <= busy_until:
            continue
        m = mkt.get(day)
        if m is None or (pit == "tw50" and h.is_member(code, day) is not True):
            continue
        a = s + 1 - DAILY_WINDOW
        daily = {"closes": cl[a:s + 1], "highs": hi[a:s + 1], "lows": lo[a:s + 1], "opens": op[a:s + 1],
                 "volumes": vo[a:s + 1], "dates": ds[a:s + 1], "current_price": cl[s],
                 "change_pct": (cl[s] / cl[s - 1] - 1) * 100 if cl[s - 1] else 0.0, "ticker": ticker, "tf_key": "daily"}
        tf = {"daily": daily}
        if wd:
            wi = bisect.bisect_right(wd, day) - 1            # 當週可能還沒走完：只取已完整結束的週線
            if wi >= 125:
                b = max(0, wi - WEEKLY_WINDOW)
                tf["weekly"] = {k: w[k][b:wi] for k in ("closes", "highs", "lows", "opens", "volumes")}
        if size_cat == "auto":
            # 與線上相同的規模分類公式與掃描門檻，但用「訊號日當時」的收盤與 20 日均量（張）計算
            vol_lots = sum(vo[s - 19:s + 1]) / 20          # _fetch_long_history_tw 的成交量單位已是「張」
            if cl[s] < 5 or vol_lots < min_vol:
                continue
            info["size_cat"] = su._classify_size(cl[s], vol_lots)
            info["volume_lots"] = vol_lots
        mo, status = _overview(m)
        bump = 5 if status == "caution" else 0
        if config.is_earnings_season(datetime.strptime(day, "%Y-%m-%d"))["in_season"]:
            bump = max(bump, 5)
        se.THRESH["min_score"] = min(75, base_min + bump) if bump else base_min     # 與 scanner._check_market_status 相同
        sig = se.generate_signal_tw(ticker, info, tf, mo)
        if not sig:
            continue
        after = [{"bar_date": ds[i], "open": op[i], "high": hi[i], "low": lo[i], "close": cl[i]}
                 for i in range(s + 1, min(len(cl), s + 1 + 40))]
        row = {"ticker": ticker, "signal_date": day, "size_cat": info["size_cat"],
               "direction": sig["direction"], "score": sig["score"],
               "grade": sig["grade"], "adx": sig.get("adx_value"), "rsi": sig.get("rsi_value"),
               "vol_ratio": sig.get("vol_ratio"), "weekly_bias": sig.get("weekly_bias"),
               "chase_level": sig.get("chase_level"), "ext_atr": sig.get("ext_atr"),
               "chg_pct": round(daily["change_pct"], 2), "sl_pct": sig.get("sl_pct"), "sl_atr": sig.get("sl_atr"),
               "rr1": sig.get("rr1"), "min_score_used": se.THRESH["min_score"],
               "regime": m["regime"].get("trend"), "twii_chg": round(m["chg"], 2),
               "entry_plan": sig["entry_price"], "stop": sig["stop_loss"], "tp1": sig["tp1"], "tp2": sig["tp2"],
               "tp3": sig["tp3"]}
        fill = after[0]["open"]
        risk = abs(sig["entry_price"] - sig["stop_loss"])
        row.update(fill_date=after[0]["bar_date"], fill=fill,
                   gap_pct=round((fill / sig["entry_price"] - 1) * 100, 2) if fill else None)
        sgn = 1 if sig["direction"] == "buy" else -1
        for k in (5, 10, 20):
            if fill and len(after) >= k:
                row[f"fwd{k}_pct"] = round(sgn * (after[k - 1]["close"] / fill - 1) * 100, 3)
                if k in m.get("fwd", {}):
                    row[f"mkt{k}_pct"] = round(m["fwd"][k], 3)
        if fill and risk > 0:
            # 來回成本（滑價＋手續費＋證交稅，影子口徑）換算成 R：停損距離愈窄，成本占 R 的比重愈高
            row["cost_r"] = round(-pnl_fn(fill, fill, sig["direction"], 3000, days_held=0) / (risk * 3000), 3)
        if not fill or risk <= 0 or execution.entry_blocked_by_limit(sig["direction"], after[0], cl[s]):
            row.update(status="unfilled_limit")
            rows.append(row)
            continue
        sim = execution.simulate_staged_exit(after, entry=fill, sl=sig["stop_loss"], tp1=sig["tp1"], tp2=sig["tp2"],
                                             tp3=sig["tp3"], direction=sig["direction"], shares=3000, gen_date=day,
                                             pnl_fn=pnl_fn, expire_days=expire, prev_close=cl[s])
        if not sim:
            continue
        one = execution.simulate_exit(planned_entry=sig["entry_price"], entry_fill=fill, stop=sig["stop_loss"],
                                      direction=sig["direction"], bars_after=after, bar_date=day,
                                      expire_days=expire, risk=risk, tp1=sig["tp1"], tp2=sig["tp2"], tp3=sig["tp3"],
                                      now=datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1), prev_close=cl[s])
        open_end = execution.staged_open_at_end(sim, day, expire)
        row.update(status="provisional" if open_end else "final", result=sim["result"],
                   legs="+".join(l["stage"] for l in sim["legs"]), exit_date=sim["exit_date"],
                   hold_days=(datetime.strptime(sim["exit_date"], "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days,
                   champion_r=round(sim["pnl"] / (risk * 3000), 3), mfe_r=one.get("mfe"), mae_r=one.get("mae"),
                   tp1_before_sl=one.get("tp1_before_sl"))
        rows.append(row)
        busy_until = sim["exit_date"]            # 出場前不重複進場（線上排除已有未平倉訊號的標的）
    se.THRESH["min_score"] = base_min
    return ticker, rows, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--universe", default="tw50_pit", help="tw50_pit 或檔案路徑（每行一個代碼，如 2330.TW）")
    ap.add_argument("--size-cat", default="大型股", help="大型股／中型股／小型股，或 auto（依訊號日當時的收盤與均量分類）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="champion_replay.csv")
    a = ap.parse_args()
    if a.universe == "tw50_pit":
        import tw50_history as h
        tickers, pit = h.tickers(h.members_between(a.start, a.end)), "tw50"
    else:
        tickers, pit = [x.strip() for x in open(a.universe, encoding="utf-8") if x.strip()], None
    mkt = _market_maps(a.start)
    t0 = time.time()
    jobs = [(t, a.start, a.end, a.size_cat, pit, mkt) for t in tickers]
    n_sig, no_data = 0, []
    with open(a.out, "w", newline="", encoding="utf-8") as f, Pool(a.workers) as pool:
        wr = csv.DictWriter(f, fieldnames=FIELDS)
        wr.writeheader()
        for i, (tk, rows, st) in enumerate(pool.imap_unordered(replay_ticker, jobs), 1):
            if st != "ok":
                no_data.append(tk)
            for r in rows:
                wr.writerow({k: r.get(k) for k in FIELDS})
            n_sig += len(rows)
            print(f"[{i}/{len(jobs)}] {tk} {st} signals={len(rows)} total={n_sig} {time.time() - t0:.0f}s", flush=True)
    print(f"DONE universe={len(tickers)} pit={pit} signals={n_sig} no_data={no_data} secs={time.time() - t0:.0f}")


if __name__ == "__main__":
    main()
