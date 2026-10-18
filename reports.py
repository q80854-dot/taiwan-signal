"""每日盤前／盤後市場報告（2026-10-16）。

使用者要求：每天一份詳細的盤前（08:45 前發佈）與盤後（21:30 後發佈）報告，
涵蓋美股、原油、匯率、利率等國際市場數據與台股盤後，並做分析，發佈在網頁上。

設計原則（對應使用者「我不要資訊錯誤」）：
  1. 數字一律來自已驗證的來源（官方 TWSE/TPEX、yfinance），每項都帶來源與時間；
     抓不到就明確標「暫無」，絕不編造。
  2. 「分析敘述」採混合模式：先把上面這些**已驗證的數字**整理成事實清單，再交給
     Claude API 只負責「把這些事實串成分析」，並在 system/prompt 明確要求不得自創
     任何數字、不得臆測來源外的事件。沒有 API 金鑰或 AI 失敗時，自動退回規則式
     摘要（_rule_narrative），報告照常產生、只是分析較簡略，並標明來源。
  3. 第一版聚焦可靠市場數據（美股／原油／匯率／台股）；消息面與經濟數據日曆之後再加。
"""
from __future__ import annotations
import os
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

TZ8 = timezone(timedelta(hours=8))

# ★ 修正：2026-10-18——使用者不想被 ANTHROPIC_API_KEY 綁住。AI 分析改走 llm.py 的
# 多供應商介面：免費的 Gemini／Groq 擇一（有設金鑰就用），都沒設時自動退回規則式
# 摘要。模型標籤改用 llm.provider_name()（例如 gemini/groq/anthropic）。


def _has_api_key() -> bool:
    import llm
    return llm.available()


def _provider() -> str | None:
    import llm
    return llm.provider_name()


# ────────────────────────────────────────────────
# 事實蒐集（全部來自已驗證來源）
# ────────────────────────────────────────────────
def _pct(v):
    try:
        return f"{v:+.2f}%"
    except Exception:
        return "—"


def _num(v, nd=2):
    try:
        return f"{v:,.{nd}f}"
    except Exception:
        return "—"


def _collect_news(limit: int = 14) -> list:
    """盤面消息面：當日上市公司重大訊息公告（官方 TWSE t187ap04_L，真實資料）。
    取最新的若干則，負面關鍵字優先露出，供 AI 整理成『消息面』分析。"""
    try:
        import fundamentals
        rows = fundamentals.fetch_all_news_rows() or []
    except Exception as e:
        logger.warning(f"reports._collect_news: {e}")
        return []
    rows = sorted(rows, key=lambda r: (r.get("date", ""), r.get("time", "")), reverse=True)
    neg = [r for r in rows if r.get("negative")]
    rest = [r for r in rows if not r.get("negative")]
    picked, seen = [], set()
    for r in (neg + rest):
        k = (r.get("code"), r.get("subject"))
        if k in seen:
            continue
        seen.add(k)
        picked.append({"code": r.get("code", ""), "name": r.get("name", ""),
                       "subject": r.get("subject", ""), "negative": bool(r.get("negative")),
                       "date": r.get("date", ""), "time": r.get("time", "")})
        if len(picked) >= limit:
            break
    return picked


def _collect_facts(kind: str) -> dict:
    """蒐集報告所需的已驗證數字。回傳結構化 dict（含每項來源）。"""
    import data_fetcher as df
    facts = {"kind": kind, "sections": []}

    g = {}
    try:
        g = df.fetch_global_markets(force=True) or {}
    except Exception as e:
        logger.warning(f"reports._collect_facts global: {e}")

    def item(key, label, nd=2, pct=True, unit=""):
        v = g.get(key) or {}
        price = v.get("price")
        ok = price is not None and v.get("source") != "error"
        return {
            "label": label,
            "value": (f"{_num(price, nd)}{unit}" if ok else "暫無"),
            "chg": (_pct(v.get("chg")) if ok and v.get("chg") is not None else ""),
            "raw": (price if ok else None),
            "raw_chg": (v.get("chg") if ok else None),
            "source": v.get("source", "error"),
        }

    us = [item("sp500", "標普 500"), item("nasdaq", "那斯達克100"),
          item("dow", "道瓊工業"), item("sox", "費城半導體")]
    commodities = [item("wti", "WTI 原油", unit=" 美元"), item("brent", "布蘭特原油", unit=" 美元"),
                   item("gold", "黃金", unit=" 美元"), item("btc", "比特幣", nd=0, unit=" 美元")]
    rates_fx = [item("us10y", "美債10年殖利率", nd=3, unit="%"), item("dxy", "美元指數"),
                item("usdtwd", "美元/台幣", nd=3), item("vix", "VIX 波動率")]

    # 台股盤後（官方）
    tw = []
    tw.append(item("twii", "加權指數"))
    tw.append(item("tpex", "櫃買指數"))
    try:
        ov = df.fetch_market_overview() or {}
    except Exception as e:
        logger.warning(f"reports._collect_facts overview: {e}")
        ov = {}
    fo = (ov.get("foreign") or {})
    if fo.get("net_buy_twd") is not None:
        net_yi = fo["net_buy_twd"] / 1e8  # 元 → 億
        tw.append({"label": "外資買賣超", "value": f"{net_yi:+,.0f} 億", "chg": "",
                   "raw": round(net_yi, 1), "raw_chg": None, "source": "twse"})
    sent = ov.get("sentiment_score")
    if sent is not None:
        tw.append({"label": "市場情緒分數", "value": f"{sent}（{ov.get('sentiment_zh','')}）",
                   "chg": "", "raw": sent, "raw_chg": None, "source": "derived"})

    facts["sections"] = [
        {"id": "us", "title": "美股四大指數", "items": us},
        {"id": "commodities", "title": "原油與商品", "items": commodities},
        {"id": "rates_fx", "title": "利率與匯率", "items": rates_fx},
        {"id": "taiwan", "title": "台股盤後", "items": tw},
    ]
    facts["missing"] = [it["label"] for s in facts["sections"] for it in s["items"]
                        if it.get("source") == "error" or it.get("value") == "暫無"]
    facts["news"] = _collect_news()
    return facts


# ────────────────────────────────────────────────
# 把事實整理成給 AI 的純文字（只含已驗證數字）
# ────────────────────────────────────────────────
def _facts_as_text(facts: dict) -> str:
    lines = []
    for sec in facts["sections"]:
        lines.append(f"【{sec['title']}】")
        for it in sec["items"]:
            chg = f"（{it['chg']}）" if it.get("chg") else ""
            lines.append(f"  - {it['label']}：{it['value']}{chg}")
    if facts.get("missing"):
        lines.append(f"（本次無法取得、不得提及具體數字的項目：{'、'.join(facts['missing'])}）")
    news = facts.get("news") or []
    if news:
        lines.append("")
        lines.append("【今日重大訊息公告（官方，可作為消息面依據；僅能依這些標題，不得擴充未提及的細節）】")
        for n in news:
            tag = "（負面）" if n.get("negative") else ""
            lines.append(f"  - {n.get('code','')} {n.get('name','')}：{n.get('subject','')}{tag}")
    return "\n".join(lines)


# ────────────────────────────────────────────────
# AI 分析（混合模式：只把已驗證數字串成分析）
# ────────────────────────────────────────────────
_SYSTEM = (
    "你是一位嚴謹的台股市場分析師，為一個台股波段研究平台撰寫每日市場報告。"
    "鐵律：你只能根據『使用者訊息中明確提供的數字與事實』撰寫分析，"
    "絕對不可以自己編造、推估或引用任何沒有提供的數字、價格、百分比、事件或新聞。"
    "若某項資料標示為『暫無』，就不要提及它的具體數值，可以說明該項資料本次缺漏。"
    "用繁體中文、專業但好讀的語氣。不要給出投資建議或買賣指示，只做客觀的市場解讀與觀察。"
)


def _ai_narrative(kind: str, facts: dict) -> str | None:
    if not _has_api_key():
        return None
    import llm
    label = "盤前" if kind == "premarket" else "盤後"
    scope = ("這是『盤前』報告（台北時間早上發佈），重點在隔夜美股、原油、匯率、利率的變化，"
             "以及它們對今天台股開盤可能的影響。") if kind == "premarket" else \
            ("這是『盤後』報告（台北時間晚上發佈），重點在今天台股盤後數字的解讀，"
             "並結合最新的國際市場數據，展望後市。")
    user_msg = (
        f"請撰寫今天的台股{label}市場深度分析報告。{scope}\n\n"
        f"以下是今天蒐集到、已驗證的市場數據與消息（你只能根據這些內容撰寫，"
        f"不得引用任何沒有提供的數字、事件或新聞）：\n\n"
        f"{_facts_as_text(facts)}\n\n"
        "請寫出一份『有觀點、有脈絡、讀起來像專業投顧晨報／收盤報告』的詳細分析，"
        "而不是把數字再唸一次。用以下結構，每段都要把數字之間的關聯、因果與對台股的意涵講清楚：\n\n"
        "【今日重點摘要】3-5 句，先講今天最關鍵的 2-3 件事與整體基調（偏多／偏空／中性）。\n"
        "【國際股市】美股四大指數與費半：解讀強弱輪動、科技股氛圍，以及對台股電子權值的連動意義。\n"
        "【原油與商品】原油、黃金、比特幣：解讀其反映的通膨、避險或風險偏好訊號。\n"
        "【利率與匯率】美債10年殖利率、美元指數、美元/台幣：解讀資金面鬆緊、外資匯入匯出壓力與對台股的影響。\n"
        "【台股解讀】結合上述國際環境與台股盤後數字（指數、外資買賣超、市場情緒），"
        "判斷台股目前所處的位置與多空力道。\n"
        "【消息面】挑出上面『重大訊息公告』中較值得注意的幾則（特別是負面或影響較大的），"
        "說明其可能的意涵；若今日沒有特別重大的，就如實說明今日公告以例行性為主。\n"
        "【後市觀察與風險】列出 3-5 個接下來值得留意的方向或風險（例如待觀察的技術位置、"
        "資金面變化、國際事件的後續），但不得捏造任何未提供的具體事件或數據。\n\n"
        "語氣專業、邏輯清楚、段落之間要有連貫性。務必全程只根據上面提供的內容，不要自己補數字。"
    )
    return llm.complete(_SYSTEM, user_msg, max_tokens=6000)


# ────────────────────────────────────────────────
# 規則式摘要（沒有 AI 時的備援，數字 100% 準）
# ────────────────────────────────────────────────
def _trend_word(chg):
    if chg is None:
        return "資料暫無"
    if chg >= 1.5:
        return "明顯走高"
    if chg >= 0.2:
        return "小幅上漲"
    if chg <= -1.5:
        return "明顯走弱"
    if chg <= -0.2:
        return "小幅下跌"
    return "大致持平"


def _find(facts, label):
    for s in facts["sections"]:
        for it in s["items"]:
            if it["label"] == label:
                return it
    return None


def _rule_narrative(kind: str, facts: dict) -> str:
    label = "盤前" if kind == "premarket" else "盤後"
    sp = _find(facts, "標普 500"); sox = _find(facts, "費城半導體")
    wti = _find(facts, "WTI 原油"); gold = _find(facts, "黃金")
    us10 = _find(facts, "美債10年殖利率"); dxy = _find(facts, "美元指數"); fx = _find(facts, "美元/台幣")
    vix = _find(facts, "VIX 波動率")
    twii = _find(facts, "加權指數"); foreign = _find(facts, "外資買賣超"); sent = _find(facts, "市場情緒分數")
    out = ["（本段為規則式自動摘要；數字皆取自官方／市場即時來源，未經 AI 潤飾）"]

    # 今日重點摘要：以美股、外資、情緒合成一個整體基調
    tone = 0
    if sp and sp.get("raw_chg") is not None:
        tone += 1 if sp["raw_chg"] > 0.2 else -1 if sp["raw_chg"] < -0.2 else 0
    if foreign and foreign.get("raw") is not None:
        tone += 1 if foreign["raw"] > 0 else -1
    if sent and sent.get("raw") is not None:
        tone += 1 if sent["raw"] >= 60 else -1 if sent["raw"] < 40 else 0
    tone_zh = "偏多" if tone >= 2 else "偏空" if tone <= -2 else "中性偏多" if tone == 1 else "中性偏空" if tone == -1 else "中性"
    bits = []
    if sp and sp.get("raw_chg") is not None:
        bits.append(f"隔夜美股{_trend_word(sp['raw_chg'])}")
    if foreign and foreign.get("raw") is not None:
        bits.append(f"外資{'買超' if foreign['raw']>=0 else '賣超'} {abs(foreign['raw']):,.0f} 億")
    if sent and sent.get("raw") is not None:
        bits.append(f"市場情緒 {sent['value']}")
    out.append(f"【今日重點摘要】綜合研判今日台股盤面基調偏向「{tone_zh}」。" +
               ("主要觀察：" + "、".join(bits) + "。" if bits else ""))

    if sp and sp.get("raw_chg") is not None:
        s = (f"【國際股市】標普 500 {sp['value']}（{sp['chg']}），{_trend_word(sp['raw_chg'])}；"
             + (f"費半 {sox['chg']}，{_trend_word(sox.get('raw_chg'))}，" if sox and sox.get('raw_chg') is not None else ""))
        if sox and sox.get("raw_chg") is not None:
            s += ("半導體走強通常有利台股電子權值。" if sox["raw_chg"] > 0.5 else
                  "半導體走弱，台股電子權值恐承壓。" if sox["raw_chg"] < -0.5 else "對台股電子股連動中性。")
        out.append(s)
    if wti and wti.get("raw_chg") is not None:
        s = f"【原油與商品】WTI 原油 {wti['value']}（{wti['chg']}），{_trend_word(wti['raw_chg'])}；"
        if gold and gold.get("raw") is not None:
            s += f"黃金 {gold['value']}（{gold['chg']}）。"
        s += ("油價走高可能加重通膨疑慮。" if wti["raw_chg"] > 1.5 else
              "油價回落有助緩解通膨壓力。" if wti["raw_chg"] < -1.5 else "")
        out.append(s)
    seg = []
    if us10 and us10.get("raw") is not None:
        seg.append(f"美債10年殖利率 {us10['value']}")
    if dxy and dxy.get("raw") is not None:
        seg.append(f"美元指數 {dxy['value']}（{dxy['chg']}）")
    if fx and fx.get("raw") is not None:
        seg.append(f"美元/台幣 {fx['value']}")
    if seg:
        s = "【利率與匯率】" + "、".join(seg) + "。"
        if dxy and dxy.get("raw_chg") is not None and dxy["raw_chg"] > 0.3:
            s += "美元走強，須留意外資匯出與新台幣貶值壓力。"
        elif dxy and dxy.get("raw_chg") is not None and dxy["raw_chg"] < -0.3:
            s += "美元走弱，資金面對台股相對友善。"
        out.append(s)
    seg2 = []
    if twii and twii.get("raw") is not None:
        seg2.append(f"加權指數 {twii['value']}（{twii['chg']}），{_trend_word(twii.get('raw_chg'))}")
    if foreign and foreign.get("raw") is not None:
        seg2.append(f"外資{('買超' if foreign['raw']>=0 else '賣超')} {abs(foreign['raw']):,.0f} 億")
    if sent and sent.get("raw") is not None:
        seg2.append(f"市場情緒分數 {sent['value']}")
    if seg2:
        out.append(f"【台股{label}解讀】" + "；".join(seg2) + "。")

    # 消息面：列出今日重大訊息公告
    news = facts.get("news") or []
    if news:
        neg = [n for n in news if n.get("negative")]
        lines = ["【消息面】今日重大訊息公告摘要（官方）："]
        for n in news[:6]:
            lines.append(f"・{n.get('code','')} {n.get('name','')}：{n.get('subject','')}{'（負面）' if n.get('negative') else ''}")
        if neg:
            lines.append(f"其中 {len(neg)} 則屬負面性質，相關個股與族群宜留意。")
        out.append("\n".join(lines))

    # 後市觀察：由規則推出的提醒
    watch = []
    if vix and vix.get("raw") is not None and vix["raw"] >= 25:
        watch.append(f"VIX {vix['value']} 偏高，市場波動加大、追高宜謹慎")
    if foreign and foreign.get("raw") is not None and foreign["raw"] < 0:
        watch.append("外資呈賣超，資金面偏空，留意權值股賣壓")
    if sent and sent.get("raw") is not None and sent["raw"] < 40:
        watch.append("市場情緒偏弱，部位控管宜保守、減少同向重押")
    if us10 and us10.get("raw") is not None and us10["raw"] >= 4.5:
        watch.append(f"美債殖利率 {us10['value']} 偏高，對成長股評價構成壓力")
    if not watch:
        watch.append("目前市場數據未出現明顯極端訊號，延續上述基調觀察即可")
    out.append("【後市觀察與風險】" + "；".join(watch) + "。")

    if facts.get("missing"):
        out.append(f"【資料缺漏】本次無法取得：{'、'.join(facts['missing'])}（寧可標示暫無，也不以舊值或推估值充數）。")
    return "\n".join(out)


# ────────────────────────────────────────────────
# 產生／讀取報告
# ────────────────────────────────────────────────
def _meta_key(kind: str, date_str: str) -> str:
    return f"report_{kind}_{date_str}"


def build_report(kind: str, store=None) -> dict:
    """產生一份報告並存入 DB。kind in {premarket, postmarket}。回傳報告 dict。"""
    assert kind in ("premarket", "postmarket")
    if store is None:
        from state_store import store as _store
        store = _store
    now = datetime.now(TZ8)
    date_str = now.strftime("%Y-%m-%d")
    facts = _collect_facts(kind)
    narrative = _ai_narrative(kind, facts)
    narrative_source = "ai"
    if not narrative:
        narrative = _rule_narrative(kind, facts)
        narrative_source = "rule"
    report = {
        "kind": kind,
        "date": date_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "facts": facts,
        "narrative": narrative,
        "narrative_source": narrative_source,
        "model": (_provider() if narrative_source == "ai" else None),
        "missing": facts.get("missing", []),
    }
    try:
        store.set_meta(_meta_key(kind, date_str), json.dumps(report, ensure_ascii=False))
        store.set_meta(f"report_{kind}_latest", json.dumps(report, ensure_ascii=False))
    except Exception as e:
        logger.error(f"build_report 存檔失敗: {e}")
    logger.info(f"[報告] 已產生 {kind} 報告（{narrative_source}），缺漏 {len(report['missing'])} 項")
    return report


def get_report(kind: str, store=None) -> dict | None:
    if store is None:
        from state_store import store as _store
        store = _store
    try:
        raw = store.get_meta(f"report_{kind}_latest", "")
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"get_report({kind}): {e}")
        return None


# ────────────────────────────────────────────────
# 個股 AI 財報／基本面整理
# ────────────────────────────────────────────────
def _collect_stock_facts(code: str) -> dict:
    """蒐集單一個股的已驗證基本面資料（估值、月營收、獲利能力、公司概況、消息面）。
    全部來自既有的官方資料抓取；抓不到的欄位省略或標示未提供，不編造。"""
    f = {"code": code, "name": "", "fields": {}, "news": []}

    def g(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            logger.warning(f"_collect_stock_facts {getattr(fn,'__name__',fn)}({code}): {e}")
            return None

    try:
        from stock_universe import get_stock_info_any
        info = get_stock_info_any(code + ".TW") or get_stock_info_any(code + ".TWO") or {}
    except Exception:
        info = {}
    f["name"] = info.get("name", "") or ""
    if info:
        f["fields"]["基本資料"] = {k: info.get(k) for k in ("name", "sector", "market", "close", "change_pct") if info.get(k) is not None}

    import fundamentals as fd
    val = g(lambda: fd.fetch_valuation_map().get(code)) or {}
    if val:
        f["fields"]["估值面"] = {k: val.get(k) for k in ("pe", "pb", "yield_pct") if val.get(k) is not None}
    rev = g(lambda: fd.fetch_monthly_revenue_map().get(code)) or {}
    if rev:
        f["fields"]["月營收"] = {k: rev.get(k) for k in ("period", "yoy_pct", "mom_pct", "cum_yoy_pct") if rev.get(k) is not None}
    pq = g(fd.get_profitability_quality, code) or {}
    if pq:
        f["fields"]["獲利能力（季）"] = {k: pq.get(k) for k in
            ("period", "gross_margin_pct", "operating_margin_pct", "gross_margin_chg", "operating_margin_chg") if pq.get(k) is not None}
    prof = g(lambda: __import__("company_info").get_profile(code)) or {}
    if isinstance(prof, dict) and prof:
        keep = {k: prof.get(k) for k in ("industry", "capital", "chairman", "business", "listing_date", "established") if prof.get(k)}
        if keep:
            f["fields"]["公司概況"] = keep
    try:
        rows = fd.fetch_all_news_rows() or []
        f["news"] = [{"subject": r.get("subject", ""), "date": r.get("date", ""), "negative": bool(r.get("negative"))}
                     for r in rows if r.get("code") == code][:8]
    except Exception:
        f["news"] = []
    f["missing"] = [k for k in ("估值面", "月營收", "獲利能力（季）") if k not in f["fields"]]
    return f


def _stock_facts_text(f: dict) -> str:
    lines = [f"股票：{f.get('code','')} {f.get('name','')}"]
    for sec, kv in f.get("fields", {}).items():
        lines.append(f"【{sec}】")
        for k, v in kv.items():
            lines.append(f"  - {k}：{v}")
    if f.get("news"):
        lines.append("【近期重大訊息公告（官方）】")
        for n in f["news"]:
            lines.append(f"  - {n.get('date','')} {n.get('subject','')}{'（負面）' if n.get('negative') else ''}")
    if f.get("missing"):
        lines.append(f"（本次無法取得、不得提及具體數字的項目：{'、'.join(f['missing'])}）")
    return "\n".join(lines)


_STOCK_SYSTEM = (
    "你是一位嚴謹的台股基本面分析師。鐵律：只能根據使用者提供的『已驗證數字與公告』撰寫，"
    "絕對不可編造、推估或引用任何沒有提供的財務數字、價格、事件或新聞；標示為『未提供』的項目"
    "不要給具體數值。用繁體中文、專業好讀，客觀分析，不給買賣建議。"
)


def _stock_ai_narrative(f: dict) -> str | None:
    if not _has_api_key():
        return None
    import llm
    user_msg = (
        f"請為這檔台股撰寫一份『AI 財報／基本面整理』，整合下列已驗證資料做深度解讀"
        f"（只能用這些內容，不得補充未提供的數字或事件）：\n\n{_stock_facts_text(f)}\n\n"
        "請用以下結構，每段都要有分析而非複述：\n"
        "【公司概況】所屬產業與營運定位（依提供資料）。\n"
        "【營收動能】依月營收年增／月增／累計年增，判斷成長趨勢與是否為一次性。\n"
        "【獲利能力】依單季毛利率／營業利益率及其季變化，判斷獲利體質與趨勢。\n"
        "【評價水位】依本益比／股價淨值比／殖利率，判斷目前評價是偏高、合理或偏低。\n"
        "【消息面】整理近期重大訊息公告的意涵（特別是負面者）；若無則說明以例行公告為主。\n"
        "【綜合評估與觀察重點】歸納基本面強弱與 3-5 個後續值得追蹤的重點（不得捏造）。\n"
        "語氣專業、邏輯連貫，全程只根據提供的內容。"
    )
    return llm.complete(_STOCK_SYSTEM, user_msg, max_tokens=5000)


def build_stock_report(code: str, store=None) -> dict:
    """產生單一個股的 AI 財報整理，當日快取於 DB（避免重複花 AI 成本）。"""
    if store is None:
        from state_store import store as _store
        store = _store
    now = datetime.now(TZ8)
    date_str = now.strftime("%Y-%m-%d")
    f = _collect_stock_facts(code)
    narrative = _stock_ai_narrative(f)
    source = "ai"
    if not narrative:
        source = "rule"
        parts = [f"（本段為規則式摘要；數字皆取自官方來源，未經 AI 潤飾）"]
        kv = f.get("fields", {})
        if "月營收" in kv:
            r = kv["月營收"]
            parts.append(f"【營收動能】最新月營收期間 {r.get('period','')}，單月年增 {r.get('yoy_pct')}%、"
                         f"月增 {r.get('mom_pct')}%、累計年增 {r.get('cum_yoy_pct')}%。")
        if "獲利能力（季）" in kv:
            q = kv["獲利能力（季）"]
            parts.append(f"【獲利能力】{q.get('period','')} 毛利率 {q.get('gross_margin_pct')}%、"
                         f"營業利益率 {q.get('operating_margin_pct')}%。")
        if "估值面" in kv:
            val = kv["估值面"]
            parts.append(f"【評價水位】本益比 {val.get('pe')}、股價淨值比 {val.get('pb')}、殖利率 {val.get('yield_pct')}%。")
        if f.get("news"):
            parts.append("【消息面】近期公告：" + "；".join(n.get("subject", "") for n in f["news"][:3]) + "。")
        if f.get("missing"):
            parts.append(f"【資料缺漏】本次無法取得：{'、'.join(f['missing'])}。")
        narrative = "\n".join(parts)
    report = {
        "code": code, "name": f.get("name", ""), "date": date_str,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "fields": f.get("fields", {}), "news": f.get("news", []),
        "narrative": narrative, "narrative_source": source,
        "model": (_provider() if source == "ai" else None), "missing": f.get("missing", []),
    }
    try:
        store.set_meta(f"stock_report_{code}_{date_str}", json.dumps(report, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"build_stock_report 存檔失敗: {e}")
    return report


def get_stock_report(code: str, store=None) -> dict | None:
    """回傳今天已產生的個股報告（若有），否則 None（由呼叫端決定是否即時產生）。"""
    if store is None:
        from state_store import store as _store
        store = _store
    date_str = datetime.now(TZ8).strftime("%Y-%m-%d")
    try:
        raw = store.get_meta(f"stock_report_{code}_{date_str}", "")
        return json.loads(raw) if raw else None
    except Exception:
        return None
