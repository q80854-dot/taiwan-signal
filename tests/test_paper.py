"""Paper 帳戶模擬（Phase 5 第一刀，2026-11-09）。

重點驗證「組合入場容量/現金會計」這一層：既有 realized 曲線把所有已結算 pnl 無腦相加會高估，
Paper 模擬會因『同時持倉上限/現金上限』擋掉進不了場的訊號。純離線、不碰 Champion。
"""
import paper


def _sig(sid, entry_day, exit_day, pv, pnl, result="tp1"):
    return {"id": sid, "code": str(sid), "name": f"股{sid}",
            "generated_at": f"2026-10-{entry_day:02d}T08:00", "closed_at": f"2026-10-{exit_day:02d}T13:30",
            "position_value": pv, "pnl_twd": pnl, "result": result}


def test_capacity_limit_skips_overlapping_signals():
    # 6 檔在同一天同時入場、都還沒出場 → 持倉上限 5 → 第 6 檔進不了場
    sigs = [_sig(i, 1, 20, 100000, 5000) for i in range(6)]
    out = paper.simulate_paper_portfolio(sigs, start_capital=500000, max_positions=5)
    assert out["taken"] == 5
    assert out["skipped_capacity"] == 1


def test_cash_limit_blocks_entry():
    # 現金 150k、每檔 100k → 最多只進 1 檔（第2檔現金不足、且尚未有人出場釋放）
    sigs = [_sig(1, 1, 20, 100000, 10000), _sig(2, 1, 20, 100000, 10000)]
    out = paper.simulate_paper_portfolio(sigs, start_capital=150000, max_positions=5)
    assert out["taken"] == 1 and out["skipped_capacity"] == 1


def test_accounting_and_return():
    # 兩檔不重疊、各賺 5000 → 都進得了場、報酬 = 10000 / 500000 = 2%
    sigs = [_sig(1, 1, 5, 100000, 5000), _sig(2, 6, 10, 100000, 5000)]
    out = paper.simulate_paper_portfolio(sigs, start_capital=500000, max_positions=5)
    assert out["taken"] == 2 and out["skipped_capacity"] == 0
    assert out["final_equity"] == 510000
    assert out["total_return_pct"] == 2.0
    assert out["win_rate"] == 100.0


def test_exit_frees_slot_for_later_entry():
    # 5 檔先佔滿、其中 1 檔先出場釋放額度，之後第 6 檔才入場 → 全部進得了場
    sigs = [_sig(i, 1, 3, 90000, 1000) for i in range(5)]      # day1 入、day3 出
    sigs.append(_sig(99, 4, 10, 90000, 2000))                   # day4 入（此時已有空位）
    out = paper.simulate_paper_portfolio(sigs, start_capital=500000, max_positions=5)
    assert out["taken"] == 6 and out["skipped_capacity"] == 0


def test_daily_mtm_interpolates_unrealized():
    # 一檔：2026-10-05(週一)入場、10-09(週五)出場、賺 10000。逐日 MTM 應從期初線性爬升到期末。
    sigs = [_sig(1, 5, 9, 100000, 10000)]
    out = paper.simulate_paper_portfolio(sigs, start_capital=500000, max_positions=5)
    curve = out["daily_equity_curve"]
    assert len(curve) >= 3                       # 週一~週五逐日
    assert curve[0]["equity"] == 500000          # 入場當日尚未累積
    assert curve[-1]["equity"] == 510000         # 出場日已實現全額
    mids = [p["equity"] for p in curve[1:-1]]
    assert all(500000 < m < 510000 for m in mids)  # 中間為線性累積的浮動損益
    assert out["max_concurrent"] == 1


def test_empty_is_safe():
    out = paper.simulate_paper_portfolio([], start_capital=500000)
    assert out["taken"] == 0 and out["final_equity"] == 500000 and out["equity_curve"] == []


def test_research_only_and_missing_fields_skipped():
    sigs = [{"id": 1, "research_only": True, "generated_at": "2026-10-01T08:00",
             "closed_at": "2026-10-05T13:30", "position_value": 100000, "pnl_twd": 5000},
            {"id": 2, "generated_at": "2026-10-01T08:00", "pnl_twd": 5000}]  # 缺 closed_at
    out = paper.simulate_paper_portfolio(sigs, start_capital=500000)
    assert out["taken"] == 0
