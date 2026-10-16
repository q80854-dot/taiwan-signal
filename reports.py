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

# Claude API 設定：預設用 Claude Opus 5.5（最強），可用環境變數改成較省成本的
# 模型（例如 claude-sonnet-5-5 / claude-haiku-5-5）。沒有金鑰就走規則式。
_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5"
_EFFORT = os.environ.get("REPORT_AI_EFFORT", "medium").strip() or "medium"


def _has_api_key() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


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
    try:
        import anthropic
    except Exception as e:
        logger.warning(f"reports._ai_narrative: anthropic 套件不可用 {e}")
        return None
    label = "盤前" if kind == "premarket" else "盤後"
    scope = ("這是『盤前』報告（台北時間早上發佈），重點在隔夜美股、原油、匯率、利率的變化，"
             "以及它們對今天台股開盤可能的影響。") if kind == "premarket" else \
            ("這是『盤後』報告（台北時間晚上發佈），重點在今天台股盤後數字的解讀，"
             "並結合最新的國際市場數據，展望後市。")
    user_msg = (
        f"請撰寫今天的台股{label}市場分析報告。{scope}\n\n"
        f"以下是今天蒐集到、已驗證的市場數據（只能用這些數字）：\n\n"
        f"{_facts_as_text(facts)}\n\n"
        "請用以下結構撰寫（每段都要有實質分析，不要只是複述數字）：\n"
        "【國際盤勢】綜合美股四大指數、費半的表現與背後意義。\n"
        "【原油與商品】原油、黃金等走勢與其隱含的通膨／避險訊號。\n"
        "【利率與匯率】美債殖利率、美元指數、美元/台幣對資金面的影響。\n"
        "【台股解讀】結合上述國際環境與台股盤後數字（指數、外資、情緒）做整體解讀。\n"
        "【後市觀察】點出接下來值得留意的方向（不得捏造未提供的事件或數據）。\n"
        "每段 2-4 句，務必只根據上面提供的數字。"
    )
    try:
        client = anthropic.Anthropic()
        kwargs = dict(model=_MODEL, max_tokens=4000,
                      system=_SYSTEM,
                      messages=[{"role": "user", "content": user_msg}])
        # effort 僅新世代模型支援；舊模型忽略即可（用 try 包住）
        try:
            resp = client.messages.create(output_config={"effort": _EFFORT}, **kwargs)
        except Exception:
            resp = client.messages.create(**kwargs)
        if getattr(resp, "stop_reason", None) == "refusal":
            logger.warning("reports._ai_narrative: 模型拒答，退回規則式")
            return None
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        return text or None
    except Exception as e:
        logger.warning(f"reports._ai_narrative: Claude API 失敗，退回規則式 {e}")
        return None


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
    out = [f"（本段為規則式自動摘要；數字皆取自官方／市場即時來源，未經 AI 潤飾）"]
    sp = _find(facts, "標普 500"); sox = _find(facts, "費城半導體")
    if sp and sp.get("raw_chg") is not None:
        out.append(f"【國際盤勢】標普 500 {sp['value']}（{sp['chg']}），{_trend_word(sp['raw_chg'])}；"
                   + (f"費半 {sox['chg']}，{_trend_word(sox.get('raw_chg'))}。" if sox and sox.get('raw_chg') is not None else "費半資料本次暫無。"))
    wti = _find(facts, "WTI 原油"); gold = _find(facts, "黃金")
    if wti and wti.get("raw_chg") is not None:
        out.append(f"【原油與商品】WTI 原油 {wti['value']}（{wti['chg']}），{_trend_word(wti['raw_chg'])}；"
                   + (f"黃金 {gold['value']}（{gold['chg']}）。" if gold and gold.get('raw') is not None else "黃金資料本次暫無。"))
    us10 = _find(facts, "美債10年殖利率"); dxy = _find(facts, "美元指數"); fx = _find(facts, "美元/台幣")
    seg = []
    if us10 and us10.get("raw") is not None:
        seg.append(f"美債10年殖利率 {us10['value']}")
    if dxy and dxy.get("raw") is not None:
        seg.append(f"美元指數 {dxy['value']}（{dxy['chg']}）")
    if fx and fx.get("raw") is not None:
        seg.append(f"美元/台幣 {fx['value']}")
    if seg:
        out.append("【利率與匯率】" + "、".join(seg) + "。")
    twii = _find(facts, "加權指數"); foreign = _find(facts, "外資買賣超"); sent = _find(facts, "市場情緒分數")
    seg2 = []
    if twii and twii.get("raw") is not None:
        seg2.append(f"加權指數 {twii['value']}（{twii['chg']}），{_trend_word(twii.get('raw_chg'))}")
    if foreign and foreign.get("raw") is not None:
        seg2.append(f"外資{('買超' if foreign['raw']>=0 else '賣超')} {abs(foreign['raw']):,.0f} 億")
    if sent and sent.get("raw") is not None:
        seg2.append(f"市場情緒分數 {sent['value']}")
    if seg2:
        out.append(f"【台股{label}解讀】" + "；".join(seg2) + "。")
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
        "model": (_MODEL if narrative_source == "ai" else None),
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
