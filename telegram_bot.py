"""
telegram_bot.py v2.0
新增：
★ 盤前集結建議（08:45）— 完整早報格式
★ 盤後集結建議（16:45）— 完整選股報告 + 明日計畫
★ 富邦連線狀態顯示
★ 已持倉追蹤提醒
"""
import logging, requests, time, json, os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_FREE_CHANNEL, TELEGRAM_PAID_CHANNEL,
    TELEGRAM_CONFIG, DISCLAIMER, SYSTEM,
)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
SUBSCRIBERS_PATH = "instance/subscribers.json"

# ════════════════════════════════════════════════
# 訂閱者管理
# ════════════════════════════════════════════════
def _load_subscribers() -> Dict:
    if os.path.exists(SUBSCRIBERS_PATH):
        with open(SUBSCRIBERS_PATH, "r") as f:
            return json.load(f)
    return {"free": [], "paid": [], "admin": [TELEGRAM_CHAT_ID]}

def _save_subscribers(data: Dict):
    os.makedirs("instance", exist_ok=True)
    with open(SUBSCRIBERS_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_subscriber(chat_id: str, tier: str = "free"):
    subs = _load_subscribers()
    chat_id = str(chat_id)
    if chat_id not in subs.get(tier, []):
        subs.setdefault(tier, []).append(chat_id)
        _save_subscribers(subs)

def remove_subscriber(chat_id: str):
    subs = _load_subscribers()
    chat_id = str(chat_id)
    for tier in subs:
        if chat_id in subs[tier]:
            subs[tier].remove(chat_id)
    _save_subscribers(subs)

def is_paid_subscriber(chat_id: str) -> bool:
    return str(chat_id) in _load_subscribers().get("paid", [])

# ════════════════════════════════════════════════
# 發送
# ════════════════════════════════════════════════
def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    # ★ 修正：2026-08-30——原本 TOKEN 沒設定、或 Telegram 回非 200（常見原因：
    #   使用者封鎖了 bot、chat_id 不存在、訊息裡有 HTML 特殊字元導致 parse_mode=HTML
    #   解析失敗、訊息太長、被限流），都只是安靜回傳 False，broadcast() 那邊只會看到
    #   一個彙總的「幾成功/幾總數」，完全看不出「哪一個訂閱者、為什麼失敗」，
    #   跟今天抓到的其他靜默失敗是同一種模式。這裡把 Telegram 實際回傳的錯誤內容
    #   記下來，之後真的有人反映「收不到訊息」才有辦法直接從 log 查出原因。
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("send_message: TELEGRAM_BOT_TOKEN 未設定，訊息不會送出")
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": str(chat_id), "text": text,
                  "parse_mode": parse_mode, "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"send_message chat_id={chat_id}: HTTP {r.status_code} - {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        logger.error(f"send_message: {e}")
        return False

def broadcast(text: str, tier: str = "free") -> int:
    subs = _load_subscribers()
    targets = (
        list(set(subs.get("free", []) + subs.get("paid", [])))
        if tier == "all" else subs.get(tier, [])
    )
    success = 0
    for chat_id in targets:
        if send_message(chat_id, text):
            success += 1
        time.sleep(0.05)
    logger.info(f"廣播 [{tier}] {success}/{len(targets)}")
    return success

# ════════════════════════════════════════════════
# 盤前集結建議（08:45）
# ════════════════════════════════════════════════
def send_morning_brief(market_overview: Dict):
    """
    盤前完整早報：
    - 美股昨收 + 亞股期貨
    - 外資昨日動向
    - 富邦連線狀態
    - 今日重點觀察股（上次訊號追蹤）
    - 操作建議
    """
    now_tw = datetime.now(timezone(timedelta(hours=8)))
    idx    = market_overview.get("index", {})
    sp500  = idx.get("sp500", {})
    nasdaq = idx.get("nasdaq", {})
    vix    = idx.get("vix", {})
    dxy    = idx.get("dxy", {})
    twii   = idx.get("twii", {})
    foreign = market_overview.get("foreign", {})
    score   = market_overview.get("sentiment_score", 50)
    source  = market_overview.get("data_source", "yfinance")

    # 情緒燈號
    if score >= 70:   emoji_s = "🟢"; mood = "看多"
    elif score >= 50: emoji_s = "🟡"; mood = "中性偏多"
    elif score >= 30: emoji_s = "🟠"; mood = "中性偏空"
    else:             emoji_s = "🔴"; mood = "看空"

    # 美股格式
    def fmt_idx(d, decs=2):
        if not d: return "—"
        p = d.get("price", 0); chg = d.get("chg", 0)
        arrow = "▲" if chg >= 0 else "▼"
        color_open = "" if chg >= 0 else ""
        return f"{p:,.{decs}f} {arrow}{abs(chg):.2f}%"

    # 外資
    fn = foreign.get("net_buy_twd", 0) or 0
    fn_str = f"{'買超' if fn >= 0 else '賣超'} {abs(fn)/1e8:.1f}億"
    fn_emoji = "💚" if fn >= 0 else "❤️"

    # 大盤狀態建議
    status = market_overview.get("market_status", "normal")
    op_advice = {
        "stop":    "🔴 大盤重挫，今日以觀望為主，避免追高",
        "caution": "🟠 大盤偏弱，謹慎操作，控制倉位",
        "normal":  "🟢 大盤正常，依訊號操作，注意個股量能",
    }.get(status, "")

    # 富邦連線狀態
    from data_fetcher import get_fubon_connection_status
    fb = get_fubon_connection_status()
    fb_str = "✅ 富邦 Neo 已連線（盤中即時數據）" if fb["connected"] else "📡 yfinance 模式（收盤數據）"

    # ── 免費版早報 ──
    msg_free = (
        f"🌅 <b>盤前集結建議</b> {now_tw.strftime('%m/%d（%A）')}\n"
        f"⏰ 台北時間 {now_tw.strftime('%H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"{emoji_s} <b>市場情緒：{mood}</b>（{score}/100）\n"
        f"\n"
        f"🌏 <b>昨日美股收盤</b>\n"
        f"S&P 500：{fmt_idx(sp500)}\n"
        f"Nasdaq：{fmt_idx(nasdaq)}\n"
        f"VIX：{fmt_idx(vix, 1)}\n"
        f"美元指數：{fmt_idx(dxy, 2)}\n"
        f"\n"
        f"🇹🇼 <b>加權指數</b>\n"
        f"昨收：{twii.get('price', '—'):,.0f}（{'+' if twii.get('chg',0) >= 0 else ''}{twii.get('chg',0):.2f}%）\n"
        f"\n"
        f"👥 <b>外資昨日動向</b>\n"
        f"{fn_emoji} {fn_str}\n"
        f"\n"
        f"📋 <b>今日操作建議</b>\n"
        f"{op_advice}\n"
        f"\n"
        f"📡 數據來源：{fb_str}\n"
        f"\n"
        f"<i>盤後 16:45 發送選股訊號</i>\n"
        f"<i>💎 付費版查看重點個股追蹤</i>"
    )

    broadcast(msg_free, tier="free")

    # ── 付費版（額外加持倉追蹤）──
    from state_store import store
    pending = store.get_pending_signals()
    if pending:
        tracking = "\n".join(
            f"  • {s['name']}（{s['code']}）{s['direction_zh']} "
            f"進場 {s['entry_price']:.2f} / SL {s['stop_loss']:.2f} / TP1 {s['tp1']:.2f}"
            for s in pending[:5]
        )
        msg_paid_extra = (
            f"\n📊 <b>持倉追蹤（{len(pending)} 筆進行中）</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"{tracking}\n\n"
            f"<i>今日盤中請留意止損位</i>"
        )
        broadcast(msg_free + msg_paid_extra, tier="paid")
    else:
        broadcast(msg_free, tier="paid")

    logger.info("盤前集結建議已發送")

# ════════════════════════════════════════════════
# 盤後集結建議（16:45）
# ════════════════════════════════════════════════
def send_daily_report(signals: List[Dict], market_overview: Dict, scan_stats: Dict):
    """
    盤後完整報告：
    - 今日大盤表現
    - 外資 + 法人動向
    - 今日選股訊號
    - 明日操作計畫
    - 已持倉更新
    """
    now_tw = datetime.now(timezone(timedelta(hours=8)))
    idx    = market_overview.get("index", {})
    twii   = idx.get("twii", {})
    vix    = idx.get("vix", {})
    foreign = market_overview.get("foreign", {})
    score   = market_overview.get("sentiment_score", 50)

    buy_sigs  = [s for s in signals if s["direction"] == "buy"]
    sell_sigs = [s for s in signals if s["direction"] == "sell"]
    top3      = sorted(signals, key=lambda x: x["score"], reverse=True)[:3]

    # 大盤表現
    twii_chg = twii.get("chg", 0)
    twii_str = (
        f"{twii.get('price', 0):,.0f}點 "
        f"（{'+' if twii_chg >= 0 else ''}{twii_chg:.2f}%，"
        f"{'+' if twii.get('chg_pt',0) >= 0 else ''}{twii.get('chg_pt',0):,.0f}點）"
    ) if twii else "—"

    # 外資動向
    fn = foreign.get("net_buy_twd", 0) or 0
    fn_str = f"{'買超' if fn >= 0 else '賣超'} {abs(fn)/1e8:.1f}億"
    fn_emoji = "💚" if fn >= 0 else "❤️"

    # 今日市場評語
    if twii_chg > 1.5:   market_comment = "大盤強勁上漲，多頭氣氛濃厚"
    elif twii_chg > 0.3:  market_comment = "大盤小幅上漲，偏多格局"
    elif twii_chg > -0.3: market_comment = "大盤盤整，方向未明"
    elif twii_chg > -1.5: market_comment = "大盤小幅下跌，注意風險"
    else:                  market_comment = "大盤重挫，謹慎保守"

    # 明日操作計畫
    tomorrow_plan = _generate_tomorrow_plan(signals, market_overview)

    # 前三強訊號
    top3_str = "\n".join(
        f"  {i+1}. {s.get('emoji_grade','')} <b>{s['name']}（{s['code']}）</b> "
        f"{'📈' if s['direction']=='buy' else '📉'} {s['score']}分\n"
        f"     進場 {s.get('entry_price',0):.2f} | SL {s['stop_loss']:.2f} | TP1 {s['tp1']:.2f}"
        for i, s in enumerate(top3)
    ) if top3 else "  今日無高分訊號"

    # ── 免費版盤後報告 ──
    msg_free = (
        f"📊 <b>盤後集結建議</b> {now_tw.strftime('%m/%d')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"🇹🇼 <b>今日大盤</b>\n"
        f"加權指數：{twii_str}\n"
        f"評語：{market_comment}\n"
        f"VIX：{vix.get('price', '—'):.1f}\n"
        f"\n"
        f"👥 <b>外資動向</b>\n"
        f"{fn_emoji} 外資：{fn_str}\n"
        f"\n"
        f"📡 <b>今日選股結果</b>\n"
        f"共掃描 {scan_stats.get('scanned', 0)} 檔，找到 <b>{len(signals)}</b> 個訊號\n"
        f"📈 做多：{len(buy_sigs)} 個　📉 做空：{len(sell_sigs)} 個\n"
        f"\n"
        f"🏆 <b>今日最強訊號（前3）</b>\n"
        f"{top3_str}\n"
        f"\n"
        f"📋 <b>明日操作計畫</b>\n"
        f"{tomorrow_plan['brief']}\n"
        f"\n"
        f"⏰ 耗時 {scan_stats.get('duration_min', 0):.1f} 分鐘\n"
        f"<i>💎 付費版查看完整分析 + 建議張數 + 法人動向</i>"
    )

    broadcast(msg_free, tier="free")
    time.sleep(0.5)

    # ── 付費版完整盤後報告 ──
    if signals:
        all_sigs_str = "\n".join(
            f"{i+1}. {s.get('emoji_grade','')} {s['name']}（{s['code']}）"
            f"{'📈' if s['direction']=='buy' else '📉'} {s['score']}分 "
            f"SL {s.get('sl_pct',0):.1f}% TP1+{s.get('tp1_pct',0):.1f}% "
            f"建議{s.get('suggested_lots',1)}張"
            for i, s in enumerate(signals)
        )
        msg_paid = (
            msg_free +
            f"\n\n━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>付費版完整清單</b>\n\n"
            f"{all_sigs_str}\n\n"
            f"<b>明日完整計畫</b>\n"
            f"{tomorrow_plan['full']}\n\n"
            f"<i>⚠️ {DISCLAIMER}</i>"
        )
        broadcast(msg_paid, tier="paid")

    # 管理員同步
    subs = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, f"[管理員] 盤後報告已發出 | {len(signals)} 個訊號 | 掃描 {scan_stats.get('scanned',0)} 檔")

    logger.info(f"盤後集結建議已發送：{len(signals)} 個訊號")


def _generate_tomorrow_plan(signals: List[Dict], market_overview: Dict) -> Dict:
    """產生明日操作計畫文字"""
    score   = market_overview.get("sentiment_score", 50)
    status  = market_overview.get("market_status", "normal")
    fn      = market_overview.get("foreign", {}).get("net_buy_twd", 0) or 0
    buy_cnt = len([s for s in signals if s["direction"] == "buy"])
    top_sig = max(signals, key=lambda x: x["score"], default=None) if signals else None

    # 整體方向
    if score >= 65 and fn >= 0 and status == "normal":
        direction = "偏多操作"
        strategy  = "可積極建立多頭部位，量能放大才追"
    elif score >= 50:
        direction = "中性觀察"
        strategy  = "選擇強勢股分批進場，嚴格執行停損"
    else:
        direction = "偏空謹慎"
        strategy  = "以觀望為主，避免逆勢操作"

    # 具體建議
    if top_sig:
        brief = (
            f"方向：{direction}\n"
            f"重點：{top_sig['name']}（{top_sig['code']}）評分{top_sig['score']}分，"
            f"明日注意進場區 {top_sig.get('entry_zone_low',0):.2f}~{top_sig.get('entry_zone_high',0):.2f}\n"
            f"策略：{strategy}"
        )
        full = (
            f"整體方向：{direction}\n"
            f"市場情緒：{score}/100\n"
            f"外資動向：{'買超' if fn>=0 else '賣超'}{abs(fn)/1e8:.1f}億\n\n"
            f"今日找到 {buy_cnt} 個做多機會\n"
            f"建議操作：{strategy}\n\n"
            f"重點標的：\n" + "\n".join(
                f"  • {s['name']} 進場 {s.get('entry_zone_low',0):.2f}~{s.get('entry_zone_high',0):.2f} "
                f"SL {s['stop_loss']:.2f}"
                for s in signals[:5]
            )
        )
    else:
        brief = f"方向：{direction}\n策略：今日無明確訊號，明日持續觀察\n{strategy}"
        full  = brief

    return {"brief": brief, "full": full}

# ════════════════════════════════════════════════
# 個別訊號推播
# ════════════════════════════════════════════════
def format_signal_free(sig: Dict) -> str:
    dir_emoji  = "📈 做多" if sig["direction"] == "buy" else "📉 做空"
    grade_emoji = {"A": "🔥", "B": "✅", "C": "👀"}.get(sig.get("grade", "C"), "📊")
    return (
        f"{grade_emoji} <b>{sig['name']}（{sig.get('code', sig['ticker'])}）</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"方向：{dir_emoji}\n"
        f"現價：<b>{sig['current_price']:.2f}</b> "
        f"（{'+' if sig.get('change_pct',0)>=0 else ''}{sig.get('change_pct',0):.2f}%）\n\n"
        f"📍 進場區：{sig.get('entry_zone_low',0):.2f} ~ {sig.get('entry_zone_high',0):.2f}\n"
        f"🛑 止損：{sig['stop_loss']:.2f}（-{sig.get('sl_pct',0):.1f}%）\n"
        f"🎯 TP1：{sig['tp1']:.2f}（+{sig.get('tp1_pct',0):.1f}%）\n\n"
        f"📝 {sig.get('reason_brief', '—')}\n\n"
        f"📊 評分：{sig['score']}分（{sig.get('grade','C')}級）｜{sig.get('sector','—')}\n"
        f"<i>💎 付費版：完整三段止盈 + 建議張數 + 法人動向</i>"
    )

def format_signal_paid(sig: Dict) -> str:
    dir_emoji  = "📈 做多" if sig["direction"] == "buy" else "📉 做空"
    grade_emoji = {"A": "🔥", "B": "✅", "C": "👀"}.get(sig.get("grade", "C"), "📊")
    weekly_zh  = {"bullish": "週線多頭✓", "bearish": "週線空頭", "neutral": "週線橫盤"}.get(
        sig.get("weekly_bias", "neutral"), "—")
    conds_met  = "\n".join(f"  ✅ {c}" for c in sig.get("conditions_met", [])[:5])
    conds_fail = "\n".join(f"  ⚠️ {c}" for c in sig.get("conditions_fail", [])[:3])
    return (
        f"{grade_emoji} <b>{sig['name']}（{sig.get('ticker','')}）</b> — {sig.get('action','')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"方向：{dir_emoji}｜產業：{sig.get('sector','—')}\n"
        f"現價：<b>{sig['current_price']:.2f}</b> （{'+' if sig.get('change_pct',0)>=0 else ''}{sig.get('change_pct',0):.2f}%）\n\n"
        f"💰 <b>進出場計劃</b>\n"
        f"📍 進場區：{sig.get('entry_zone_low',0):.2f} ~ {sig.get('entry_zone_high',0):.2f}\n"
        f"🛑 止損：{sig['stop_loss']:.2f}（-{sig.get('sl_pct',0):.1f}%）\n"
        f"🎯 TP1：{sig['tp1']:.2f}（+{sig.get('tp1_pct',0):.1f}%）1:{sig.get('rr1',1.5)} ← 出1/3\n"
        f"🎯 TP2：{sig.get('tp2',0):.2f}（+{sig.get('tp2_pct',0):.1f}%）1:{sig.get('rr2',2.5)} ← 出1/3\n"
        f"🎯 TP3：{sig.get('tp3',0):.2f}（+{sig.get('tp3_pct',0):.1f}%）1:{sig.get('rr3',4.0)} ← 出1/3\n\n"
        f"📦 <b>倉位</b>\n"
        f"建議：<b>{sig.get('suggested_lots',1)} 張</b>\n"
        f"風險：TWD {sig.get('risk_twd',0):,}（{sig.get('risk_pct',0):.1f}%）\n"
        f"費稅：TWD {sig.get('roundtrip_cost',0):,}\n\n"
        f"📊 <b>指標</b>\n"
        f"評分：{sig['score']}分（{sig.get('grade','C')}）｜{weekly_zh}\n"
        f"ADX {sig.get('adx_value',0):.0f} / RSI {sig.get('rsi_value',50):.0f} / 量比 {sig.get('vol_ratio',1):.1f}x\n\n"
        + (f"✅ 確認條件：\n{conds_met}\n" if conds_met else "")
        + (f"⚠️ 注意：\n{conds_fail}\n\n" if conds_fail else "\n")
        + f"👥 法人：{sig.get('inst_signal','尚未取得')}\n"
        f"💡 {sig.get('reason_full','—')}\n\n"
        f"⏰ 有效 {sig.get('expire_days',3)} 個交易日｜"
        f"{datetime.now(timezone(timedelta(hours=8))).strftime('%m/%d %H:%M')}\n"
        f"<i>⚠️ {DISCLAIMER}</i>"
    )

def push_signal(sig: Dict):
    free_msg = format_signal_free(sig)
    if TELEGRAM_FREE_CHANNEL: send_message(TELEGRAM_FREE_CHANNEL, free_msg)
    else: broadcast(free_msg, tier="free")
    time.sleep(0.3)
    paid_msg = format_signal_paid(sig)
    if TELEGRAM_PAID_CHANNEL: send_message(TELEGRAM_PAID_CHANNEL, paid_msg)
    else: broadcast(paid_msg, tier="paid")
    subs = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, f"[訊號] {sig.get('name','')} {sig.get('direction','')} score={sig['score']}")
    logger.info(f"訊號推播：{sig.get('name','')} score={sig['score']}")

# ════════════════════════════════════════════════
# 系統警報 + Bot 指令
# ════════════════════════════════════════════════
def send_alert(message: str, level: str = "info"):
    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}.get(level, "📢")
    subs  = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, f"{emoji} <b>系統通知</b>\n{message}")

def handle_update(update: Dict) -> Optional[str]:
    msg     = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip()
    if not text or not chat_id: return None
    cmd = text.split()[0].lower()

    if cmd == "/start":
        add_subscriber(chat_id, "free")
        return (
            f"👋 歡迎！<b>{SYSTEM['name']}</b>\n\n"
            f"📅 推播時間：\n"
            f"• 08:45 盤前集結建議\n"
            f"• 16:45 盤後選股報告\n\n"
            f"/status — 今日市場\n"
            f"/signals — 今日訊號\n"
            f"/upgrade — 升級付費版\n"
            f"/help — 使用說明\n\n"
            f"<i>{DISCLAIMER}</i>"
        )
    elif cmd == "/stop":
        remove_subscriber(chat_id)
        return "已取消訂閱。感謝使用！"
    elif cmd == "/upgrade":
        return (
            "💎 <b>付費版功能</b>\n\n"
            "免費版：基本訊號 + 盤前盤後摘要\n"
            "付費版：完整三段止盈 + 建議張數 + 法人動向 + 持倉追蹤 + 明日計畫\n\n"
            "💰 月費：TWD 299\n"
            "📧 聯絡管理員升級"
        )
    elif cmd == "/help":
        return (
            f"📖 <b>使用說明</b>\n\n"
            f"每日掃描 1000+ 台股，波段策略選股。\n\n"
            f"🕗 08:45 盤前集結建議（美股昨收 + 外資 + 今日計畫）\n"
            f"🕔 16:45 盤後選股報告（技術訊號 + 明日操作）\n\n"
            f"🔥 A級（85+）強力建議\n"
            f"✅ B級（75+）良好訊號\n"
            f"👀 C級（65+）觀察機會\n\n"
            f"<i>{DISCLAIMER}</i>"
        )
    return None

def set_webhook(webhook_url: str) -> bool:
    try:
        r = requests.post(f"{BASE_URL}/setWebhook", json={"url": webhook_url}, timeout=15)
        if r.status_code == 200 and r.json().get("ok"):
            logger.info(f"Webhook 設定：{webhook_url}")
            return True
        return False
    except Exception as e:
        logger.error(f"set_webhook: {e}")
        return False

def get_bot_info() -> Dict:
    try:
        r = requests.get(f"{BASE_URL}/getMe", timeout=10)
        if r.status_code == 200: return r.json().get("result", {})
    except: pass
    return {}
