"""
系統健檢 + 下單前質檢。
每一項檢查回傳 {name, status: ok|warn|fail|info, detail}，讓網頁用同一種方式顯示。
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
TPE = timezone(timedelta(hours=8))


def _c(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _safe(fn, name):
    try:
        return fn()
    except Exception as e:
        logger.warning(f"health check {name}: {e}")
        return [_c(name, "fail", f"檢查本身發生錯誤：{e}")]


def _now_tpe():
    return datetime.now(TPE)


def _expected_trade_date():
    """目前預期官方已公布的最新交易日(週一~週五，15:00 後算當天，否則前一個週間日)。不處理國定假日。"""
    n = _now_tpe()
    d = n.date()
    if n.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def check_data():
    out = []
    from stock_universe import get_universe_data_meta
    m = get_universe_data_meta()
    exp = _expected_trade_date()
    out.append(_c("官方報價清單", "ok" if m.get("fetch_ok") else "fail",
                  "證交所／櫃買報價已取得" if m.get("fetch_ok") else "官方報價取得失敗，掃描池可能使用備援清單"))
    t, o = m.get("tse_quote_date"), m.get("otc_quote_date")
    st = "ok"
    if m.get("dates_mismatch"):
        st = "warn"
    out.append(_c("上市／上櫃報價日期", st, f"上市 {t}　上櫃 {o}　（預期最新交易日 {exp}）" + ("；兩邊日期不同" if m.get("dates_mismatch") else "")))
    from state_store import store
    cs = store.get_ohlcv_cache_stats()
    out.append(_c("K 線快取", "ok" if cs.get("tickers", 0) > 300 else "warn",
                  f"{cs.get('tickers', 0)} 檔、{cs.get('bars', 0)} 根；最後更新 {cs.get('last_updated')}"))
    from data_fetcher import fetch_ohlcv
    from stock_universe import get_stock_info_any
    for tk in ("2330.TW", "6488.TWO"):
        d = fetch_ohlcv(tk, "daily")
        info = get_stock_info_any(tk)
        if d and info and info.get("close") is not None:
            same = str(info.get("quote_date") or "")[:10] == d["dates"][-1][:10]
            diff = abs(d["closes"][-1] - float(info["close"]))
            if same:
                out.append(_c(f"{tk} 收盤對官方", "ok" if diff <= 0.011 else "fail",
                              f"{d['dates'][-1][:10]} 我們 {d['closes'][-1]}／官方 {info['close']}"))
            else:
                out.append(_c(f"{tk} 收盤對官方", "info", f"K 線最新 {d['dates'][-1][:10]}，官方報價日 {info.get('quote_date')}，日期不同故不比較"))
        else:
            out.append(_c(f"{tk} 收盤對官方", "warn", "取不到資料"))
    return out


def check_fundamentals():
    from fundamentals import fetch_monthly_revenue_map, fetch_valuation_map, material_news_fetch_ok, fetch_material_news_risk_map
    out = []
    rv = fetch_monthly_revenue_map()
    per = {}
    for v in rv.values():
        p = v.get("period")
        if p: per[p] = per.get(p, 0) + 1
    top = max(per.items(), key=lambda x: x[1]) if per else (None, 0)
    out.append(_c("月營收", "ok" if len(rv) > 1500 else "warn", f"{len(rv)} 檔；主要期別 {top[0]}（{top[1]} 檔）"))
    val = fetch_valuation_map()
    out.append(_c("本益比／淨值比／殖利率", "ok" if len(val) > 1500 else "warn", f"{len(val)} 檔"))
    ok = material_news_fetch_ok()
    n = sum(len(v) for v in fetch_material_news_risk_map().values())
    out.append(_c("重大訊息（僅上市）", "ok" if ok else "fail", f"來源正常，命中風險關鍵字 {n} 則" if ok else "取得失敗"))
    return out


def check_engine():
    out = []
    from state_store import store
    hist = store.get_scan_history(3)
    if hist:
        h = hist[0]
        out.append(_c("最近一次掃描", "ok" if not h.get("errors") else "warn",
                      f"{h.get('scan_date')}　掃描 {h.get('scanned')} 檔、找到 {h.get('signals_found')}、發送 {h.get('signals_sent')}、錯誤 {h.get('errors')}、{h.get('duration_min')} 分鐘"))
    else:
        out.append(_c("最近一次掃描", "warn", "尚無掃描紀錄"))
    a = store.get_meta("last_scan_audit")
    if a:
        out.append(_c("預篩自我稽核", "ok" if not a.get("mismatch") else "fail",
                      f"{a.get('date')}：跳過率 {a.get('skip_rate') or 0}%，抽查 {a.get('audited')} 檔、不一致 {a.get('mismatch')}；官方末棒修正 {a.get('recon_fixed')}／{a.get('recon_checked')}"))
    else:
        out.append(_c("預篩自我稽核", "info", "尚未有紀錄（下次掃描後產生）"))
    try:
        import app as A
        if A.SCHEDULER_OK and A.scheduler.running:
            jobs = A.scheduler.get_jobs()
            nxt = sorted([(j.next_run_time, j.id) for j in jobs if j.next_run_time and j.id != "heartbeat"])
            out.append(_c("排程器", "ok", f"運作中，{len(jobs)} 個任務；下一個：{nxt[0][1]} @ {nxt[0][0].strftime('%m-%d %H:%M')}" if nxt else f"運作中，{len(jobs)} 個任務"))
        else:
            out.append(_c("排程器", "fail", "未運作"))
    except Exception as e:
        out.append(_c("排程器", "warn", f"無法讀取：{e}"))
    try:
        durs = [h.get("duration_min") for h in hist if h.get("duration_min") and (h.get("scanned") or 0) > 100]
        if len(durs) >= 2 and durs[0] > 2 * (sum(durs[1:]) / len(durs[1:])):
            out.append(_c("掃描耗時", "warn", f"最近一次 {durs[0]} 分鐘，是前幾次平均的 {round(durs[0] / (sum(durs[1:]) / len(durs[1:])), 1)} 倍，請留意資料來源是否變慢"))
    except Exception:
        pass
    import admin_auth
    out.append(_c("策略學習後台保護", "ok" if admin_auth.configured() else "fail",
                  "已設定管理密碼，學習資料需登入才能查看" if admin_auth.configured() else "尚未設定 ADMIN_PASSWORD，後台目前鎖死（無法登入）"))
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    out.append(_c("Telegram 推播", "ok" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "fail",
                  "已設定" if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else "缺少 Token 或 Chat ID"))
    return out


def check_strategy():
    out = []
    from state_store import store
    rows = store.get_recent_signals(limit=200, days_back=60)
    # ★ 修正：2026-10-09——與 get_performance_summary 的勝率定義對齊：只算觸及停損/停利的
    # 決定性結果（排除 expired），並排除 ETF 空單，避免各頁停損率不一致。
    closed = [r for r in rows if r.get("result") in ("tp1", "tp2", "tp3", "sl")
              and not (r.get("direction") == "sell" and str(r.get("code", "")).startswith("00"))]
    sl = [r for r in closed if r.get("result") == "sl"]
    openp = [r for r in rows if r.get("result") == "pending" and r.get("status") == "active"]
    out.append(_c("持倉數", "warn" if len(openp) >= 5 else "ok", f"{len(openp)} 檔進行中" + ("（已達 5 檔上限，不再發新訊號）" if len(openp) >= 5 else "")))
    if len(closed) < 20:
        out.append(_c("策略樣本數", "warn", f"已結案 {len(closed)} 筆，少於 20 筆，勝率與停損率不具統計意義"))
    else:
        out.append(_c("停損率", "warn" if len(sl) / len(closed) > .5 else "ok", f"{len(sl)}／{len(closed)}＝{round(len(sl) / len(closed) * 100)}%"))
    try:
        import shadow
        shadow.ensure_table()
        with store._conn() as conn:
            r = conn.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN kind='candidate' THEN 1 ELSE 0 END) AS c, "
                             "SUM(CASE WHEN kind='control' THEN 1 ELSE 0 END) AS k, "
                             "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS d, MAX(bar_date) AS last FROM shadow_signals").fetchone()
        r = dict(r)
        out.append(_c("學習資料（影子追蹤）", "ok" if (r.get("n") or 0) > 0 else "warn",
                      f"候選 {r.get('c') or 0}、對照組 {r.get('k') or 0}、已結算 {r.get('d') or 0}；最近記錄日 {r.get('last') or '—'}" if r.get("n") else "尚無紀錄（掃描後開始累積）"))
        with store._conn() as conn:
            lr = conn.execute("SELECT * FROM shadow_runs ORDER BY id DESC LIMIT 1").fetchone()
        if lr:
            lr = dict(lr)
            out.append(_c("最近一次學習資料收集", "warn" if lr.get("errors") else "ok",
                          f"候選 {lr.get('cand_saved')}、被規則擋下 {lr.get('rej_saved')}、對照 {lr.get('ctrl_saved')}、新結算 {lr.get('res_closed')}；耗時 {lr.get('secs_total')}s"
                          + (f"；有錯誤：{lr.get('errors')[:120]}" if lr.get("errors") else "")))
        else:
            out.append(_c("最近一次學習資料收集", "info", "尚無收集紀錄（新版第一次掃描後產生）"))
    except Exception as e:
        out.append(_c("學習資料（影子追蹤）", "warn", str(e)))
    from config import THRESH, SHORT_SIGNAL_THRESH
    out.append(_c("進場門檻", "info", f"做多分數 ≥ {THRESH.get('min_score')}；做空 ≥ {SHORT_SIGNAL_THRESH['min_score']} 且週線必須偏空"))
    return out


def check_freshness():
    out = []
    try:
        from data_fetcher import inst_status
        i = inst_status()
        if i.get("at") is None:
            out.append(_c("三大法人資料", "info", "本次啟動後尚未取得（掃描開始時會先抓一次）"))
        elif i.get("ok"):
            out.append(_c("三大法人資料", "ok", f"{i.get('n')} 檔，資料日 {i.get('date')}，耗時 {i.get('secs')}s"))
        else:
            out.append(_c("三大法人資料", "fail", f"最近一次取得失敗：{i.get('err')}。這段期間的訊號沒有法人籌碼加減分"))
    except Exception as e:
        out.append(_c("三大法人資料", "warn", str(e)))
    try:
        from fundamentals import revenue_status, _rev_state
        st = revenue_status()
        e = st["expected_period"]
        if st.get("n_total"):
            pct = round(st["n_expected"] / st["n_total"] * 100)
            from fundamentals import _taipei_now
            early = _taipei_now().day <= 10     # 法定截止日(10日)前，公司陸續公告，覆蓋率低屬正常
            lvl = "ok" if pct >= 90 else ("info" if early else ("warn" if st.get("in_window") else "info"))
            out.append(_c("月營收新鮮度", lvl, f"應有 {e[:-2]}年{e[-2:]}月：全市場已有 {st['n_expected']}／{st['n_total']} 檔（{pct}%）"
                          + ("；尚未到每月 10 日法定截止，公司陸續公告中，其餘檔數沿用上一期" if early and pct < 90 else "")
                          + (f"；其中 {st['mops_added']} 檔由公開資訊觀測站補上" if st.get("mops_added") else "")
                          + ("；公告期內每 10 分鐘自動更新" if st.get("in_window") else "")))
        else:
            out.append(_c("月營收新鮮度", "info", "本次啟動後尚未載入"))
    except Exception as ex:
        out.append(_c("月營收新鮮度", "warn", str(ex)))
    return out


def run_health():
    sections = [("資料即時性", check_freshness), ("資料來源", check_data), ("基本面與消息", check_fundamentals), ("引擎與推播", check_engine), ("策略狀態", check_strategy)]
    res, cnt = [], {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for title, fn in sections:
        items = _safe(fn, title)
        for i in items: cnt[i["status"]] = cnt.get(i["status"], 0) + 1
        res.append({"title": title, "items": items})
    verdict = "fail" if cnt["fail"] else ("warn" if cnt["warn"] else "ok")
    return {"generated_at": _now_tpe().strftime("%Y-%m-%d %H:%M:%S"), "verdict": verdict, "counts": cnt, "sections": res}


# ───────────── 下單前質檢 ─────────────
def check_signal(key):
    """key: 代號或 ticker。以最近的有效訊號為對象，逐項驗證能否依此下單。"""
    from state_store import store
    from stock_universe import get_stock_info_any
    code = key.split(".")[0]
    rows = [r for r in store.get_recent_signals(limit=120, days_back=30) if r.get("code") == code or (r.get("ticker") or "").startswith(code)]
    if not rows:
        return {"error": f"找不到 {code} 近 30 天的訊號"}
    s = rows[0]
    tk = s.get("ticker")
    buy = s.get("direction") == "buy"
    out = []
    info = get_stock_info_any(tk) or {}
    last = info.get("close")
    entry, sl = s.get("entry_price"), s.get("stop_loss")

    # 1 價格對官方
    try:
        from data_fetcher import fetch_ohlcv
        d = fetch_ohlcv(tk, "daily")
        if d and last is not None:
            same = str(info.get("quote_date") or "")[:10] == d["dates"][-1][:10]
            if same:
                diff = abs(d["closes"][-1] - float(last))
                out.append(_c("價格與官方一致", "ok" if diff <= .011 else "fail", f"K 線 {d['closes'][-1]}／官方 {last}"))
            else:
                out.append(_c("價格與官方一致", "info", f"日期不同（K 線 {d['dates'][-1][:10]}／官方 {info.get('quote_date')}）"))
        else:
            out.append(_c("價格與官方一致", "warn", "無法取得比對資料"))
    except Exception as e:
        out.append(_c("價格與官方一致", "warn", str(e)))

    # 2 訊號新鮮度與狀態
    gen = (s.get("generated_at") or "")[:10]
    status_txt = f"{s.get('status')}／{s.get('result')}"
    out.append(_c("訊號狀態", "ok" if (s.get("result") == "pending" and s.get("status") == "active") else "fail",
                  f"{gen} 產生；{status_txt}" + ("" if s.get("result") == "pending" else "（已結案，不可再進場）")))

    # 3 與進場價的距離(追價)
    if last and entry:
        drift = (float(last) / entry - 1) * 100 * (1 if buy else -1)
        st = "ok" if drift <= 1.5 else ("warn" if drift <= 3 else "fail")
        out.append(_c("現價 vs 進場價", st, f"現價 {last}、建議進場 {entry}，{'高於' if drift > 0 else '低於'}進場價 {abs(round(drift, 2))}%" + ("；若尚未進場，這筆已追高（已依訊號進場者請看持倉追蹤）" if st != "ok" else "")))
    # 4 停損結構
    if entry and sl:
        slp = abs(entry - sl) / entry * 100
        atr = s.get("sl_atr")
        ok = slp >= 1.5 and (atr is None or atr >= 1.5)
        out.append(_c("停損距離", "ok" if ok else "warn", f"{round(slp, 2)}%" + (f"、約 {atr} ATR" if atr else "") + ("" if ok else "；偏窄，容易被正常波動掃到")))
        if last and ((buy and float(last) <= sl) or (not buy and float(last) >= sl)):
            out.append(_c("是否已觸及停損", "fail", f"現價 {last} 已越過停損 {sl}"))
    # 5 風報比
    rr = s.get("rr1")
    if rr is not None:
        out.append(_c("風報比 (TP1)", "ok" if rr >= 1.5 else "warn", f"{rr}"))
    # 6 流動性
    shares = s.get("suggested_shares") or s.get("suggested_lots")  # 資料庫的 suggested_lots 欄位實際存股數
    lots, vol = (shares / 1000.0 if shares else None), info.get("volume_lots")
    if lots and vol:
        pct = lots / vol * 100
        out.append(_c("流動性", "ok" if pct <= 1 else "warn", f"建議約 {round(lots, 2)} 張，占當日成交 {round(pct, 2)}%（{vol} 張）"))
    # 7 處置/注意
    if info.get("is_disposal_or_attention"):
        out.append(_c("處置／注意股", "fail", "被列為處置或注意股，波動與流動性風險高"))
    else:
        out.append(_c("處置／注意股", "ok", "否"))
    # 7b 暫停交易／變更交易／停資停券／借券賣出
    try:
        import market_extras
        from state_store import store as _store
        _sf = market_extras.stock_flags(code, info.get("volume_lots"), _store)
        for _f in _sf["flags"]:
            if _f["key"] in ("halt", "altered", "margin_stop", "no_limit"):
                out.append(_c({"halt": "暫停交易", "altered": "變更交易", "margin_stop": "停資停券", "no_limit": "無漲跌幅"}[_f["key"]], _f["level"], _f["text"]))
        _sh = _sf.get("short")
        if _sh and _sh.get("sbl_short_chg_lots") is not None and _sh.get("short_vs_volume_days") is not None:
            _hot = _sh["short_vs_volume_days"] >= 3 and _sh["sbl_short_chg_lots"] > 0
            out.append(_c("借券賣出餘額", "warn" if _hot else "info",
                          f"借券賣出 {_sh['sbl_short_lots']} 張（{_sh['chg_days']} 日 {_sh['sbl_short_chg_lots']:+} 張），空單餘額約為成交量 {_sh['short_vs_volume_days']} 天"))
    except Exception as e:
        out.append(_c("交易限制／借券", "warn", str(e)))
    # 8 重大訊息
    try:
        from fundamentals import fetch_material_news_risk_map
        nw = fetch_material_news_risk_map().get(code, [])
        if nw:
            out.append(_c("重大訊息", "fail", "；".join(n.get("subject", "")[:30] for n in nw[:2])))
        else:
            out.append(_c("重大訊息", "ok" if tk.endswith(".TW") else "info", "無命中風險關鍵字" if tk.endswith(".TW") else "上櫃股未涵蓋重大訊息"))
    except Exception as e:
        out.append(_c("重大訊息", "warn", str(e)))
    # 9 基本面
    try:
        from fund_score import build_fundamental_profile
        p = build_fundamental_profile(code)
        fs = p.get("score")
        if fs is None:
            out.append(_c("基本面", "info", "資料涵蓋不足，未評分"))
        else:
            out.append(_c("基本面", "ok" if fs >= 50 else ("warn" if fs >= 35 else "fail"), f"{fs} 分（{p.get('grade')}）；" + "、".join((p.get("tags") or [])[:3])))
    except Exception as e:
        out.append(_c("基本面", "warn", str(e)))
    # 10 大盤環境
    try:
        from data_fetcher import fetch_market_overview
        from risk_manager import get_system_status
        ss = get_system_status(fetch_market_overview())
        out.append(_c("大盤環境", "ok" if ss.get("can_trade") else "fail", f"{ss.get('env_status') or ''}（環境分 {ss.get('env_score')}）"))
    except Exception as e:
        out.append(_c("大盤環境", "warn", str(e)))
    # 11 追高等級
    cl = s.get("chase_level")
    if cl:
        out.append(_c("追高等級", {"low": "ok", "mid": "warn", "high": "fail"}.get(cl, "info"), {"low": "低", "mid": "中", "high": "高（延伸過大）"}.get(cl, cl)))

    bad = sum(1 for i in out if i["status"] == "fail")
    warn = sum(1 for i in out if i["status"] == "warn")
    verdict = "reject" if bad else ("caution" if warn >= 2 else "pass")
    return {"ticker": tk, "name": s.get("name"), "direction": s.get("direction"), "score": s.get("score"),
            "checks": out, "verdict": verdict, "fail": bad, "warn": warn,
            "generated_at": _now_tpe().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "質檢是用即時資料重新驗證訊號是否仍可執行，不是保證獲利；最終是否下單由你決定。"}
