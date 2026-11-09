"""paper.py — 前向 Paper 帳戶模擬（Phase 5 第一刀，2026-11-09）。

把系統「實際發出並已結算」的訊號，依時間與「持倉/資金上限」模擬一個 Paper 帳戶，回答：
「如果我每一筆訊號都在能力範圍內照做，帳戶會怎麼走？」

與既有程式的分工（刻意不重造輪子）：
- 單筆分批出場與成本：沿用 scanner 結算時已算好的 pnl_twd（來自 execution.simulate_staged_exit，
  與回測同一套），本模組不重算出場。
- 本模組只加「組合層的入場容量/現金會計」：在每筆的入場時刻檢查『同時持倉數上限』與『現金上限』，
  決定哪些訊號實際進得了場。既有的 realized 權益曲線（state_store）是把所有已結算訊號的 pnl
  無腦相加、未考慮你同時只能持有 N 檔與現金有限——那會高估；本模組修正這點。

界線：純模擬、不動真錢、不碰 Champion 決策。Paper ≠ 實盤；且需真實訊號前向累積才有統計意義。
"""
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


def _pv(sig: Dict) -> float:
    """單筆進場市值（TWD）。優先用 position_value，否則 股數×進場價。"""
    pv = sig.get("position_value")
    if pv:
        return float(pv)
    shares = sig.get("suggested_shares") or sig.get("suggested_lots") or 0
    price = sig.get("entry_price") or sig.get("current_price") or 0
    return float(shares) * float(price)


def simulate_paper_portfolio(closed_signals: List[Dict], start_capital: float = 500000.0,
                             max_positions: int = 5) -> Dict:
    """以入場容量/現金上限模擬 Paper 帳戶（事件時間軸：先出場釋放額度、再入場）。

    closed_signals 每筆需有：generated_at（入場時點）、closed_at（出場時點）、pnl_twd（已結算損益），
    以及 position_value 或（suggested_shares + entry_price）。研究樣本或缺欄位者自動略過。
    回傳帳戶摘要、權益曲線（依出場日）、交易紀錄與『因容量不足未進場』的筆數。
    """
    # 整理可用樣本
    rows = []
    for s in closed_signals:
        if s.get("pnl_twd") is None or not s.get("generated_at") or not s.get("closed_at"):
            continue
        if s.get("research_only"):
            continue
        pv = _pv(s)
        if pv <= 0:
            continue
        rows.append({"id": s.get("id"), "code": s.get("code"), "name": s.get("name"),
                     "entry_t": str(s["generated_at"]), "exit_t": str(s["closed_at"]),
                     "pv": pv, "pnl": float(s["pnl_twd"]), "result": s.get("result")})
    if not rows:
        return {"start_capital": start_capital, "final_equity": start_capital, "total_return_pct": 0.0,
                "taken": 0, "skipped_capacity": 0, "win_rate": None, "max_drawdown_pct": 0.0,
                "trades": [], "equity_curve": [], "note": "尚無已結算訊號可模擬（樣本累積中）"}

    # 事件時間軸：(time, order, kind, row)；同一時刻先出場(order=0)再入場(order=1)
    events = []
    for r in rows:
        events.append((r["exit_t"], 0, "exit", r))
        events.append((r["entry_t"], 1, "entry", r))
    events.sort(key=lambda e: (e[0], e[1]))

    cash = start_capital
    open_pos: Dict = {}          # id -> pv
    taken = skipped = 0
    wins = 0
    trades: List[Dict] = []
    equity_curve: List[Dict] = []
    peak = start_capital
    max_dd = 0.0

    def equity() -> float:
        return cash + sum(open_pos.values())

    for t, _order, kind, r in events:
        if kind == "entry":
            if r["id"] in open_pos:
                continue
            if len(open_pos) < max_positions and cash >= r["pv"]:
                cash -= r["pv"]; open_pos[r["id"]] = r["pv"]; taken += 1
                r["_taken"] = True
            else:
                skipped += 1; r["_taken"] = False
        else:  # exit
            if open_pos.get(r["id"]) is not None:
                cash += r["pv"] + r["pnl"]; del open_pos[r["id"]]
                if r["pnl"] > 0:
                    wins += 1
                trades.append({"code": r["code"], "name": r["name"], "exit": r["exit_t"][:10],
                               "pnl_twd": round(r["pnl"], 0), "result": r["result"]})
                eq = equity()
                equity_curve.append({"date": r["exit_t"][:10], "equity": round(eq, 0)})
                peak = max(peak, eq)
                if peak > 0:
                    max_dd = max(max_dd, (peak - eq) / peak * 100)

    final_eq = equity()
    return {
        "start_capital": round(start_capital, 0),
        "final_equity": round(final_eq, 0),
        "total_return_pct": round((final_eq - start_capital) / start_capital * 100, 2) if start_capital else 0.0,
        "taken": taken,
        "skipped_capacity": skipped,            # 因『同時持倉/現金上限』而沒進場的訊號數
        "win_rate": round(wins / taken * 100, 1) if taken else None,
        "max_drawdown_pct": round(max_dd, 2),
        "max_positions": max_positions,
        "trades": trades[-50:],
        "equity_curve": equity_curve,
        "note": "Paper 模擬（沿用已結算 pnl ＋ 組合入場容量/現金會計）；非實盤、需樣本累積。",
    }


def paper_report(limit: int = 500, start_capital: Optional[float] = None,
                 max_positions: Optional[int] = None) -> Dict:
    """讀系統已結算訊號、跑 Paper 模擬。供端點/報告使用。"""
    try:
        from state_store import store
        from config import ACCOUNT_BALANCE_TWD, MAX_SIMULTANEOUS_POSITIONS
        rows = store.get_closed_trades(limit=limit)
        return simulate_paper_portfolio(
            rows,
            start_capital=start_capital if start_capital is not None else ACCOUNT_BALANCE_TWD,
            max_positions=max_positions if max_positions is not None else MAX_SIMULTANEOUS_POSITIONS,
        )
    except Exception as e:
        logger.error(f"paper_report: {e}")
        return {"error": "Paper 模擬失敗", "detail": str(e)}
