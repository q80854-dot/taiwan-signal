"""
telegram_bot.py — Telegram Bot 推播服務 v1.0
免費版：基本訊號  付費版：完整分析
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

# ── 訂閱者管理 ──
SUBSCRIBERS_PATH = "instance/subscribers.json"

def _load_subscribers() -> Dict:
    if os.path.exists(SUBSCRIBERS_PATH):
        with open(SUBSCRIBERS_PATH,"r") as f: return json.load(f)
    return {"free":[],"paid":[],"admin":[TELEGRAM_CHAT_ID]}

def _save_subscribers(data: Dict):
    os.makedirs("instance", exist_ok=True)
    with open(SUBSCRIBERS_PATH,"w") as f: json.dump(data,f,ensure_ascii=False,indent=2)

def add_subscriber(chat_id: str, tier: str = "free"):
    subs = _load_subscribers(); chat_id = str(chat_id)
    if chat_id not in subs.get(tier,[]):
        subs.setdefault(tier,[]).append(chat_id); _save_subscribers(subs)
        logger.info(f"新訂閱者 {chat_id} [{tier}]")

def remove_subscriber(chat_id: str):
    subs = _load_subscribers(); chat_id = str(chat_id)
    for tier in subs:
        if chat_id in subs[tier]: subs[tier].remove(chat_id)
    _save_subscribers(subs)

def is_paid_subscriber(chat_id: str) -> bool:
    return str(chat_id) in _load_subscribers().get("paid",[])

# ── 發送 ──
def send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN: return False
    try:
        r = requests.post(f"{BASE_URL}/sendMessage",
                          json={"chat_id":str(chat_id),"text":text,
                                "parse_mode":parse_mode,"disable_web_page_preview":True},
                          timeout=15)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"send_message: {e}"); return False

def broadcast(text: str, tier: str = "free") -> int:
    subs = _load_subscribers()
    targets = list(set(subs.get("free",[])+subs.get("paid",[]))) if tier=="all" else subs.get(tier,[])
    success = 0
    for chat_id in targets:
        if send_message(chat_id, text): success += 1
        time.sleep(0.05)
    logger.info(f"廣播 [{tier}] {success}/{len(targets)}")
    return success

# ── 免費版格式 ──
def format_signal_free(sig: Dict) -> str:
    dir_emoji = "📈 做多" if sig["direction"]=="buy" else "📉 做空"
    grade_emoji = {"A":"🔥","B":"✅","C":"👀"}.get(sig.get("grade","C"),"📊")
    return (
        f"{grade_emoji} <b>{sig['name']}（{sig.get('code',sig['ticker'])}）</b>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"方向：{dir_emoji}\n"
        f"現價：<b>{sig['current_price']:.2f}</b> ({'+' if sig.get('change_pct',0)>=0 else ''}{sig.get('change_pct',0):.2f}%)\n\n"
        f"📍 進場區：{sig.get('entry_zone_low',0):.2f} ~ {sig.get('entry_zone_high',0):.2f}\n"
        f"🛑 止損：{sig['stop_loss']:.2f}（-{sig.get('sl_pct',0):.1f}%）\n"
        f"🎯 目標：{sig['tp1']:.2f}（+{sig.get('tp1_pct',0):.1f}%）\n\n"
        f"📝 {sig.get('reason_brief','—')}\n\n"
        f"─────────────────\n"
        f"📊 評分：{sig['score']}分（{sig.get('grade','C')}級）　🏭 {sig.get('sector','—')}\n\n"
        f"<i>💎 付費版：完整三段止盈 + 建議張數 + 法人動向</i>\n"
        f"<i>⚠️ 僅供參考，不構成投資建議</i>"
    )

# ── 付費版格式 ──
def format_signal_paid(sig: Dict) -> str:
    dir_emoji = "📈 做多" if sig["direction"]=="buy" else "📉 做空"
    grade_emoji = {"A":"🔥","B":"✅","C":"👀"}.get(sig.get("grade","C"),"📊")
    weekly_zh = {"bullish":"週線多頭 ✓","strong_bullish":"週線強多 ✓✓","bearish":"週線空頭","neutral":"週線橫盤"}.get(sig.get("weekly_bias","neutral"),"—")
    conds_met  = "\n".join(f"  ✅ {c}" for c in sig.get("conditions_met",[])[:5])
    conds_fail = "\n".join(f"  ⚠️ {c}" for c in sig.get("conditions_fail",[])[:3])
    return (
        f"{grade_emoji} <b>{sig['name']}（{sig.get('ticker','')}）</b> — {sig.get('action','')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"方向：{dir_emoji}　產業：{sig.get('sector','—')}\n"
        f"現價：<b>{sig['current_price']:.2f}</b> ({'+' if sig.get('change_pct',0)>=0 else ''}{sig.get('change_pct',0):.2f}%)\n\n"
        f"💰 <b>進出場計劃</b>\n"
        f"📍 進場區：{sig.get('entry_zone_low',0):.2f} ~ {sig.get('entry_zone_high',0):.2f}\n"
        f"🛑 止損：{sig['stop_loss']:.2f}（-{sig.get('sl_pct',0):.1f}%）\n"
        f"🎯 TP1：{sig['tp1']:.2f}（+{sig.get('tp1_pct',0):.1f}%）1/{sig.get('rr1',1.5)} ← 出1/3\n"
        f"🎯 TP2：{sig.get('tp2',0):.2f}（+{sig.get('tp2_pct',0):.1f}%）1/{sig.get('rr2',2.5)} ← 出1/3\n"
        f"🎯 TP3：{sig.get('tp3',0):.2f}（+{sig.get('tp3_pct',0):.1f}%）1/{sig.get('rr3',4.0)} ← 出1/3\n\n"
        f"📦 <b>倉位建議</b>\n"
        f"建議張數：<b>{sig.get('suggested_lots',1)} 張</b>\n"
        f"預估風險：TWD {sig.get('risk_twd',0):,}（{sig.get('risk_pct',0):.1f}%）\n"
        f"部位市值：TWD {sig.get('position_value',0):,}\n"
        f"來回費稅：TWD {sig.get('roundtrip_cost',0):,}\n\n"
        f"📊 <b>技術指標</b>\n"
        f"評分：{sig['score']}分（{sig.get('grade','C')}級）　{weekly_zh}\n"
        f"ADX：{sig.get('adx_value',0):.0f} / RSI：{sig.get('rsi_value',50):.0f} / 量比：{sig.get('vol_ratio',1):.1f}x\n\n"
        + (f"<b>確認條件：</b>\n{conds_met}\n" if conds_met else "")
        + (f"<b>注意：</b>\n{conds_fail}\n\n" if conds_fail else "\n")
        + f"👥 法人：{sig.get('inst_signal','尚未取得')}\n\n"
        f"💡 {sig.get('reason_full','—')}\n\n"
        f"⏰ 有效期：{sig.get('expire_days',3)} 個交易日\n"
        f"📅 {datetime.now(timezone(timedelta(hours=8))).strftime('%m/%d %H:%M')}\n\n"
        f"<i>⚠️ {DISCLAIMER}</i>"
    )

# ── 推播訊號 ──
def push_signal(sig: Dict):
    free_msg = format_signal_free(sig)
    if TELEGRAM_FREE_CHANNEL: send_message(TELEGRAM_FREE_CHANNEL, free_msg)
    else: broadcast(free_msg, tier="free")
    time.sleep(0.3)
    paid_msg = format_signal_paid(sig)
    if TELEGRAM_PAID_CHANNEL: send_message(TELEGRAM_PAID_CHANNEL, paid_msg)
    else: broadcast(paid_msg, tier="paid")
    subs = _load_subscribers()
    for admin_id in subs.get("admin",[]):
        send_message(admin_id, f"[管理員] 新訊號\n{paid_msg}")
    logger.info(f"訊號推播：{sig.get('name','')} score={sig['score']}")

# ── 早盤摘要 ──
def send_morning_brief(market_overview: Dict):
    now_tw = datetime.now(timezone(timedelta(hours=8)))
    idx    = market_overview.get("index",{})
    sp500  = idx.get("sp500",{})
    nasdaq = idx.get("nasdaq",{})
    vix    = idx.get("vix",{})
    foreign= market_overview.get("foreign",{})
    score  = market_overview.get("sentiment_score",50)
    emoji  = "🟢" if score>=70 else "🟡" if score>=50 else "🔴"
    sp500_str  = f"{sp500.get('price',0):,.0f}（{'+' if sp500.get('chg',0)>=0 else ''}{sp500.get('chg',0):.2f}%）" if sp500 else "—"
    nasdaq_str = f"{nasdaq.get('price',0):,.0f}（{'+' if nasdaq.get('chg',0)>=0 else ''}{nasdaq.get('chg',0):.2f}%）" if nasdaq else "—"
    vix_str    = f"{vix.get('price',0):.1f}" if vix else "—"
    fn = foreign.get("net_buy_twd",0) or 0
    foreign_str= f"{'買超' if fn>=0 else '賣超'} {abs(fn)/1e8:.0f}億" if foreign else "更新中"
    status_zh  = {"stop":"🔴 大盤重挫，暫停做多","caution":"⚠️ 大盤偏弱，謹慎","normal":"✅ 正常交易"}.get(market_overview.get("market_status","normal"),"")
    msg = (
        f"🌅 <b>早盤摘要</b> {now_tw.strftime('%m/%d')}（{now_tw.strftime('%H:%M')} 台北）\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{emoji} 市場情緒：<b>{market_overview.get('sentiment_zh','中性')}</b>（{score}/100）\n\n"
        f"🌏 <b>美股昨收</b>\n"
        f"S&P 500：{sp500_str}\nNasdaq：{nasdaq_str}\nVIX：{vix_str}\n\n"
        f"🇹🇼 <b>外資動向（昨日）</b>\n外資：{foreign_str}\n\n"
        f"📋 <b>今日建議</b>\n{status_zh}\n\n"
        f"<i>盤後約 16:45 發送選股訊號</i>"
    )
    broadcast(msg, tier="free"); broadcast(msg, tier="paid")
    logger.info("早盤摘要已發送")

# ── 盤後報告 ──
def send_daily_report(signals: List[Dict], market_overview: Dict, scan_stats: Dict):
    now_tw = datetime.now(timezone(timedelta(hours=8)))
    idx    = market_overview.get("index",{})
    twii   = idx.get("twii",{})
    twii_str = f"{twii.get('price',0):,.0f}（{'+' if twii.get('chg',0)>=0 else ''}{twii.get('chg',0):.2f}%，{twii.get('chg_pt',0):+,.0f}點）" if twii else "—"
    buy_sigs  = [s for s in signals if s["direction"]=="buy"]
    sell_sigs = [s for s in signals if s["direction"]=="sell"]
    top3      = sorted(signals, key=lambda x:x["score"], reverse=True)[:3]
    top3_str  = "\n".join(f"  {i+1}. {s['name']}（{s.get('code','')}）{s['direction_zh']} {s['score']}分" for i,s in enumerate(top3)) if top3 else "  今日無訊號"
    msg_free = (
        f"📊 <b>盤後報告</b> {now_tw.strftime('%m/%d')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🇹🇼 加權指數：{twii_str}\n"
        f"市場情緒：{market_overview.get('sentiment_zh','—')}\n\n"
        f"📡 <b>今日訊號</b>：共 {len(signals)} 個\n"
        f"📈 做多：{len(buy_sigs)}　📉 做空：{len(sell_sigs)}\n\n"
        f"🏆 <b>最強訊號（前3）</b>：\n{top3_str}\n\n"
        f"🔍 掃描：{scan_stats.get('scanned',0)} 檔，耗時 {scan_stats.get('duration_min',0):.1f} 分鐘\n\n"
        f"<i>💎 付費版查看完整分析</i>"
    )
    broadcast(msg_free, tier="free")
    if signals:
        paid_extra = (
            f"📋 <b>今日所有訊號</b>\n━━━━━━━━━━━━━━━━━\n"
            + "\n".join(
                f"{i+1}. {s.get('emoji_grade','')} {s['name']}（{s.get('code','')}）"
                f"{'📈' if s['direction']=='buy' else '📉'} "
                f"{s['score']}分 SL={s.get('sl_pct',0):.1f}% TP1=+{s.get('tp1_pct',0):.1f}%"
                for i,s in enumerate(signals)
            )
        )
        broadcast(paid_extra, tier="paid")
    logger.info(f"盤後報告已發送，共 {len(signals)} 個訊號")

# ── 警報 ──
def send_alert(message: str, level: str = "info"):
    emoji = {"info":"ℹ️","warning":"⚠️","error":"🚨"}.get(level,"📢")
    subs  = _load_subscribers()
    for admin_id in subs.get("admin",[]):
        send_message(admin_id, f"{emoji} <b>系統通知</b>\n{message}")

# ── Bot 指令處理 ──
def handle_update(update: Dict) -> Optional[str]:
    msg     = update.get("message",{})
    chat_id = str(msg.get("chat",{}).get("id",""))
    text    = msg.get("text","").strip()
    if not text or not chat_id: return None
    cmd = text.split()[0].lower()
    if cmd=="/start":
        add_subscriber(chat_id,"free")
        return (f"👋 歡迎使用 <b>{SYSTEM['name']}</b>！\n\n"
                f"📡 已訂閱免費版，每日 16:45 推播選股訊號。\n\n"
                f"/status — 市場狀態\n/signals — 今日訊號\n/upgrade — 升級付費版\n/help — 使用說明\n\n"
                f"<i>{DISCLAIMER}</i>")
    elif cmd=="/stop":
        remove_subscriber(chat_id); return "已取消訂閱。感謝使用！"
    elif cmd=="/upgrade":
        return ("💎 <b>付費版升級</b>\n\n免費版：基本訊號\n付費版：三段止盈/張數/法人/歷史勝率\n\n💰 月費：TWD 299/月\n📧 聯絡 @admin_username 升級")
    elif cmd=="/help":
        return (f"📖 <b>使用說明</b>\n\n每日掃描全市場 1000+ 台股，以 AI 技術分析找出波段機會。\n\n"
                f"📅 08:45 早盤摘要\n📅 16:45 盤後訊號\n\n🔥 A級（85+）強力\n✅ B級（75+）良好\n👀 C級（65+）觀察\n\n<i>{DISCLAIMER}</i>")
    return None

def set_webhook(webhook_url: str) -> bool:
    try:
        r = requests.post(f"{BASE_URL}/setWebhook",json={"url":webhook_url},timeout=15)
        if r.status_code==200 and r.json().get("ok"):
            logger.info(f"Webhook 設定成功：{webhook_url}"); return True
        return False
    except Exception as e:
        logger.error(f"set_webhook: {e}"); return False

def get_bot_info() -> Dict:
    try:
        r = requests.get(f"{BASE_URL}/getMe",timeout=10)
        if r.status_code==200: return r.json().get("result",{})
    except: pass
    return {}
