"""
telegram_bot.py v3.0 — 版本 C2 格式
訊號格式：一眼看懂版（表格 + 分區清晰）
"""
import logging, requests, time, json, os, threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# ★ 修正：2026-09-19——稽核發現 _load_subscribers/_save_subscribers 是單純的
# 讀檔→改記憶體→寫檔，沒有任何鎖。如果同一個process內有兩個thread幾乎同時
# 呼叫 add_subscriber/remove_subscriber（例如兩個使用者同時操作、或webhook
# 跟排程job剛好交錯），會出現經典的 read-modify-write race：後寫入的那個會
# 覆蓋掉先寫入的那個沒讀到的變更，導致某個使用者的訂閱/退訂動作憑空消失。
# 這裡加一個 process 內的 Lock 包住「讀取整份檔案→修改→寫回」這整段操作，
# 避免同一個 process 內的兩個thread互相覆蓋（跨process/跨instance的檔案鎖
# 不在這個修正範圍內，但這個專案目前是單一Render instance，足夠處理實際
# 會發生的情境）。
_subscribers_lock = threading.Lock()

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TELEGRAM_FREE_CHANNEL, TELEGRAM_PAID_CHANNEL,
    TELEGRAM_CONFIG, DISCLAIMER, SYSTEM, is_earnings_season,
)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
SUBSCRIBERS_PATH = "instance/subscribers.json"

def _load_subscribers() -> Dict:
    # ★ 修正：2026-09-03——原本檔案存在時就直接回傳檔案內容，如果那個檔案
    # 是舊版邏輯建立的（沒有 "admin" 這個 key，或 admin 清單裡沒有
    # TELEGRAM_CHAT_ID），管理者/機主本人就永遠收不到任何推播，而且完全
    # 沒有錯誤訊息——這正是「網站看得到訊號、Telegram 卻收不到」的根因
    # 之一。改成不管檔案內容是什麼，都強制把 TELEGRAM_CHAT_ID 併進
    # admin 清單（有設定才併，避免塞進空字串），這樣機主一定會在收訊名單
    # 裡，不必依賴 instance/subscribers.json 這個在 Render 上每次重新
    # 部署就會被清空的暫存檔案有沒有剛好包含正確內容。
    if os.path.exists(SUBSCRIBERS_PATH):
        with open(SUBSCRIBERS_PATH, "r") as f:
            data = json.load(f)
    else:
        data = {"free": [], "paid": [], "admin": []}
    data.setdefault("admin", [])
    if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID) not in [str(a) for a in data["admin"]]:
        data["admin"].append(str(TELEGRAM_CHAT_ID))
    return data

def _save_subscribers(data: Dict):
    os.makedirs("instance", exist_ok=True)
    with open(SUBSCRIBERS_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_subscriber(chat_id: str, tier: str = "free"):
    with _subscribers_lock:
        subs = _load_subscribers()
        chat_id = str(chat_id)
        if chat_id not in subs.get(tier, []):
            subs.setdefault(tier, []).append(chat_id)
            _save_subscribers(subs)

def remove_subscriber(chat_id: str):
    with _subscribers_lock:
        subs = _load_subscribers()
        chat_id = str(chat_id)
        for tier in subs:
            if chat_id in subs[tier]:
                subs[tier].remove(chat_id)
        _save_subscribers(subs)

def is_paid_subscriber(chat_id: str) -> bool:
    return str(chat_id) in _load_subscribers().get("paid", [])

def get_subscriber_counts() -> Dict:
    # ★ 新增：2026-09-03——給 /api/diagnostics 用，讓機主不用連進伺服器看檔案
    # 就能確認訂閱名單狀態（尤其 admin 清單裡有沒有真的包含自己的 chat_id）。
    subs = _load_subscribers()
    return {
        "free":  len(subs.get("free", [])),
        "paid":  len(subs.get("paid", [])),
        "admin": len(subs.get("admin", [])),
        "admin_includes_owner": bool(TELEGRAM_CHAT_ID) and str(TELEGRAM_CHAT_ID) in [str(a) for a in subs.get("admin", [])],
    }

def send_message(chat_id: str, text: str, parse_mode: str = "HTML", _max_retries: int = 3) -> bool:
    # ★ 修正：2026-09-03——原本非 200 的回應完全沒有記錄任何內容，只回傳
    # False，導致「訊號有沒有真的送到 Telegram」這件事在 log 裡完全看不到、
    # 只能用猜的。現在失敗時會把 Telegram API 實際回傳的狀態碼跟錯誤內容
    # （例如 chat not found、bot was blocked by the user、Unauthorized 等）
    # 完整記錄下來，才能真正定位「網站看得到訊號、TG 卻收不到」是哪一種原因。
    #
    # ★ 修正：2026-09-16——這裡原本完全沒有重試機制，Telegram 429 限流
    # （官方文件明確會回傳 retry_after 秒數，代表「等這麼久再送一定會成功」）
    # 或暫時性網路逾時/5xx，之前都是直接放棄、回傳 False，讓呼叫端
    # （scanner._push_signals）把這檔訊號直接算失敗，即使其實只要多等
    # 幾秒重送就會成功——這是「訊號沒有及時傳到 TG」問題的另一個根因。
    # 現在改成：429 時照 Telegram 回傳的 retry_after 等待後重試；5xx 或
    # 逾時/連線錯誤這類暫時性問題用簡單的指數退避（1s, 2s, 4s）重試；
    # 4xx（除429外，例如 chat not found、Unauthorized）是永久性錯誤，
    # 重試也不會成功，直接放棄並記錄，不浪費時間。最多重試 _max_retries 次，
    # 全部失敗才真的回傳 False，交由上層決定要不要警示機主。
    if not TELEGRAM_BOT_TOKEN:
        logger.error("send_message: TELEGRAM_BOT_TOKEN 未設定，無法發送")
        return False
    if not chat_id:
        logger.error("send_message: chat_id 為空，無法發送")
        return False
    attempt = 0
    while True:
        attempt += 1
        try:
            r = requests.post(
                f"{BASE_URL}/sendMessage",
                json={
                    "chat_id":                  str(chat_id),
                    "text":                     text,
                    "parse_mode":               parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                try:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 3))
                except Exception:
                    retry_after = 3
                retry_after = min(retry_after, 30)  # 避免單一訊號卡住整個掃描流程太久
                logger.warning(f"send_message 429限流：chat_id={chat_id} 等待 {retry_after}s 後重試（第{attempt}次）")
                if attempt <= _max_retries:
                    time.sleep(retry_after)
                    continue
                logger.error(f"send_message 失敗：chat_id={chat_id} 429限流重試{_max_retries}次仍失敗")
                return False
            if r.status_code >= 500 and attempt <= _max_retries:
                backoff = 2 ** (attempt - 1)
                logger.warning(f"send_message {r.status_code}（伺服器端暫時性錯誤），{backoff}s 後重試（第{attempt}次）：chat_id={chat_id}")
                time.sleep(backoff)
                continue
            logger.error(f"send_message 失敗：chat_id={chat_id} status={r.status_code} body={r.text[:500]}")
            return False
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt <= _max_retries:
                backoff = 2 ** (attempt - 1)
                logger.warning(f"send_message 網路暫時性錯誤，{backoff}s 後重試（第{attempt}次）：chat_id={chat_id} {e}")
                time.sleep(backoff)
                continue
            logger.error(f"send_message: chat_id={chat_id} 重試{_max_retries}次後仍網路錯誤: {e}")
            return False
        except Exception as e:
            logger.error(f"send_message: chat_id={chat_id} exception={e}")
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

# ══════════════════════════════════════════════
# 版本 C2 — 免費版訊號格式
# ══════════════════════════════════════════════
# ★ 新增：2026-09-16——risk_manager.py 的模組docstring明確記錄
# check_weekend_gap 已被移除，稽核（含跟 Perplexity/ChatGPT/Gemini 三方
# 交叉比對）一致認為這是現存風控缺口之一：job_daily_scan 是唯一產生新
# 訊號的排程（週一~週五 16:30），如果訊號剛好在週四、週五（尤其連假前
# 最後一個交易日）產生，接下來到下次盤後結算之間，系統完全沒有機制能
# 提醒使用者「週末/連假期間大盤或個股可能已經跳空」。這裡先用最單純的
# 「訊號產生日是週四或週五」規則加註提醒，不依賴額外的假日資料源；
# 之後如果要更精準判斷「連假前最後一個交易日」，需要串接台股交易日曆，
# 先用這個低成本版本補上「完全沒有提示」的缺口。
def _weekend_gap_warning(now_tw: datetime) -> str:
    if now_tw.weekday() in (3, 4):  # 3=週四, 4=週五
        return (
            "⚠️ <b>週末風險提醒</b>：訊號在週四/週五產生，下次盤後結算前"
            "（含週末）系統無法監控盤中價格，若隔週一開盤跳空，可能直接"
            "穿越停損價。建議進場部位酌情減碼，或等週一開盤確認走勢再進場。\n"
        )
    return ""


# ★ 新增：2026-09-16——B1財報密集期提示（見 config.py is_earnings_season()
# 註解，三方 AI 交叉比對後排定的最高優先剩餘項目）。這裡只提示「現在是
# 全市場財報密集公告期」，不是「這檔股票確定會在這幾天公告財報」——後者
# 需要逐檔精確的財報日期資料源，目前沒有串接（見 config.py 的取捨說明），
# 提示文字刻意用「可能」而非「將會」，避免造成過度精確的錯誤印象。
def _earnings_season_warning(now_tw: datetime) -> str:
    season = is_earnings_season(now_tw)
    if season["in_season"]:
        return (
            f"⚠️ <b>財報密集期提醒</b>：現在是法定財報申報截止日（{season['deadline']}，"
            f"剩 {season['days_left']} 天）前的密集公告期，個股可能在任何時間點公告財報，"
            "公告後股價跳空的機率較平時高，且本系統無法逐檔預先得知確切公告日期，"
            "建議留意個股即將公告財報的消息，並酌情減碼或提高警覺。\n"
        )
    return ""


def _data_age_str(sig: Dict, now_tw: datetime) -> str:
    """★ 新增：2026-09-19——排版/準確性整修的一部分。之前訊息只印訊息「發送」
    時間（now_tw），使用者無法分辨這是即時報價還是昨晚就算好、拖到現在才送出
    的舊資料。這裡改用 generated_at（訊號實際產生的時間，見 signal_engine.py）
    算出資料年齡，清楚標示「資料時間」而不是「送出時間」，兩者不一定相同。"""
    gen = sig.get("generated_at")
    if not gen:
        return now_tw.strftime("%m/%d %H:%M")
    try:
        gen_dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        gen_tw = gen_dt.astimezone(timezone(timedelta(hours=8)))
        age_min = max(0, int((now_tw - gen_tw).total_seconds() // 60))
        age_str = f"（{age_min}分鐘前）" if age_min < 60 else f"（{age_min//60}小時前）"
        return gen_tw.strftime("%m/%d %H:%M") + age_str
    except Exception:
        return now_tw.strftime("%m/%d %H:%M")


# ★ 修正：2026-09-19——使用者指示「統一為付費版」：個別訊號推播不再區分
# 免費/付費兩種格式，全部收訊者（含原本的免費名單）都收到完整版內容
# （TP2/TP3、建議張數、法人動向）。原本的 format_signal_free()（帶
# <tg-spoiler>升級付費版解鎖</tg-spoiler> 鎖住 TP2/TP3 的版本）已移除，
# push_signal() 現在只用下面這個 format_signal_paid() 送給所有人。
# TELEGRAM_FREE_CHANNEL/TELEGRAM_PAID_CHANNEL、subscribers 的 free/paid
# 名單、is_paid_subscriber() 這些基礎設施刻意保留不刪，方便未來如果要
# 重新拆分等級，不用重建整套訂閱名單。
#
# ★ 修正：2026-09-19——排版整修。舊版用全形空白「　」手動對齊出一個假表格
# （「價格／漲跌幅／盈虧比」表頭 + 對應欄位）。全形空白對齊只在等寬字型下
# 才會真的對齊，Telegram 在不同裝置（iOS/Android/桌面/不同字體設定）用的是
# 非等寬字型，實際顯示出來欄位常常對不齊、看起來很亂——這正是使用者反映
# 「版面美編」的根本原因。改成每行「圖示 標籤：數值（漲跌幅）」的單欄式
# 排版，不依賴空白對齊，在任何字型/裝置下都維持一致、好讀。

# ══════════════════════════════════════════════
# 版本 C2 — 付費版完整訊號格式
# ══════════════════════════════════════════════
def format_signal_paid(sig: Dict) -> str:
    now_tw   = datetime.now(timezone(timedelta(hours=8)))
    isBuy    = sig["direction"] == "buy"
    dir_str  = "📈 做多" if isBuy else "📉 做空"
    sl_arrow = "▼" if isBuy else "▲"
    tp_arrow = "▲" if isBuy else "▼"
    grade_em = {"A": "🔥", "B": "✅", "C": "👀"}.get(sig.get("grade", "C"), "📊")
    chg      = sig.get("change_pct", 0)
    chg_str  = f"{'▲' if chg >= 0 else '▼'}{abs(chg):.2f}%"
    weekly_zh = {
        "bullish":        "週線多頭✓",
        "strong_bullish": "週線強多✓✓",
        "bearish":        "週線空頭",
        "neutral":        "週線橫盤",
    }.get(sig.get("weekly_bias", "neutral"), "—")
    conds_met  = "\n".join(f"　▪️ {c}" for c in sig.get("conditions_met",  [])[:5])
    conds_fail = "\n".join(f"　⚠️ {c}" for c in sig.get("conditions_fail", [])[:3])
    inst = sig.get("inst_signal", "") or "資料更新中"

    return (
        f"{grade_em} <b>{sig.get('grade','C')}級｜{sig['name']} {sig.get('code','')}</b>\n"
        f"{sig.get('sector','—')}｜{dir_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"現價：<b>{sig['current_price']:.2f}</b>　{chg_str}\n"
        f"📍 進場區：{sig.get('entry_zone_low',0):.2f} ~ {sig.get('entry_zone_high',0):.2f}\n"
        f"🛑 止損：<b>{sig['stop_loss']:.2f}</b>　{sl_arrow}{sig.get('sl_pct',0):.1f}%\n"
        f"🥇 TP1：<b>{sig['tp1']:.2f}</b>　{tp_arrow}{sig.get('tp1_pct',0):.1f}%　1:{sig.get('rr1',1.5)}　出1/3\n"
        f"🥈 TP2：<b>{sig.get('tp2',0):.2f}</b>　{tp_arrow}{sig.get('tp2_pct',0):.1f}%　1:{sig.get('rr2',2.5)}　出1/3\n"
        f"🥉 TP3：<b>{sig.get('tp3',0):.2f}</b>　{tp_arrow}{sig.get('tp3_pct',0):.1f}%　1:{sig.get('rr3',4.0)}　出1/3\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 <b>倉位建議</b>\n"
        f"建議：<b>{sig.get('suggested_lots',1)} 張</b>　市值 TWD {sig.get('position_value',0):,}\n"
        f"風險：TWD {sig.get('risk_twd',0):,}（{sig.get('risk_pct',0):.1f}%）\n"
        f"費稅：TWD {sig.get('roundtrip_cost',0):,}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👥 <b>法人動向</b>\n"
        f"{inst}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 <b>技術指標</b>\n"
        f"評分 {sig['score']}分｜ADX {sig.get('adx_value',0):.0f}｜"
        f"RSI {sig.get('rsi_value',50):.0f}｜量比 {sig.get('vol_ratio',1):.1f}x\n"
        f"✅ {weekly_zh}｜{sig.get('size_cat','—')}\n"
        + (f"━━━━━━━━━━━━━━━\n✅ <b>確認條件：</b>\n{conds_met}\n" if conds_met else "")
        + (f"⚠️ <b>注意：</b>\n{conds_fail}\n" if conds_fail else "")
        + f"━━━━━━━━━━━━━━━\n"
        f"💡 {sig.get('reason_brief','—')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🕐 資料時間：{_data_age_str(sig, now_tw)}\n"
        f"⏰ 訊號有效 {sig.get('expire_days',3)} 個交易日\n"
        + _weekend_gap_warning(now_tw)
        + _earnings_season_warning(now_tw)
        + f"<i>{DISCLAIMER}</i>"
    )

# ══════════════════════════════════════════════
# 盤前集結建議（08:45 台北時間）
# ══════════════════════════════════════════════
def send_morning_brief(market_overview: Dict):
    now_tw  = datetime.now(timezone(timedelta(hours=8)))
    idx     = market_overview.get("index", {})
    sp500   = idx.get("sp500", {});  nasdaq = idx.get("nasdaq", {})
    vix     = idx.get("vix",   {});  dxy    = idx.get("dxy",    {})
    twii    = idx.get("twii",  {})
    foreign = market_overview.get("foreign", {})
    score   = market_overview.get("sentiment_score", 50)

    emoji_s = "🟢" if score >= 70 else "🟡" if score >= 50 else "🟠" if score >= 30 else "🔴"
    mood    = ("強烈看多" if score >= 80 else "偏多" if score >= 60 else
               "中性"     if score >= 40 else "偏空" if score >= 20 else "強烈看空")

    def fmt(d, decs=2):
        if not d: return "—"
        p = d.get("price", 0); chg = d.get("chg", 0)
        return f"{p:,.{decs}f}　{'▲' if chg >= 0 else '▼'}{abs(chg):.2f}%"

    fn     = foreign.get("net_buy_twd", 0) or 0
    fn_str = f"{'▲買超' if fn >= 0 else '▼賣超'} {abs(fn)/1e8:.1f}億"
    fn_em  = "💚" if fn >= 0 else "❤️"

    status_zh = {
        "stop":    "🔴 大盤重挫，今日以觀望為主",
        "caution": "🟠 大盤偏弱，謹慎控制倉位",
        "normal":  "🟢 大盤正常，依訊號操作",
    }.get(market_overview.get("market_status", "normal"), "")

    from state_store import store
    pending = store.get_pending_signals()
    tracking_str = ""
    if pending:
        tracking_str = (
            f"━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>持倉追蹤（{len(pending)} 筆）</b>\n"
            + "\n".join(
                f"  ▪ {s['name']}（{s.get('code','')}）"
                f"{'📈' if s['direction']=='buy' else '📉'} "
                f"SL {s['stop_loss']:.2f}｜TP1 {s['tp1']:.2f}"
                for s in pending[:5]
            )
            + "\n⚠️ 今日請留意止損位\n"
        )

    weekdays = ['週一','週二','週三','週四','週五','週六','週日']
    msg = (
        f"🌅 <b>盤前集結建議</b> "
        f"{now_tw.strftime('%m/%d')}（{weekdays[now_tw.weekday()]}）\n"
        f"⏰ 台北時間 {now_tw.strftime('%H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{emoji_s} <b>市場情緒：{mood}</b>（{score}/100）\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🌏 <b>昨日美股收盤</b>\n"
        f"S&P 500：{fmt(sp500)}\n"
        f"Nasdaq：{fmt(nasdaq)}\n"
        f"VIX：{fmt(vix, 1)}\n"
        f"美元指數：{fmt(dxy)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🇹🇼 <b>加權指數（昨收）</b>\n"
        f"{fmt(twii, 0)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>外資昨日動向</b>\n"
        f"{fn_em} {fn_str}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{tracking_str}"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>今日操作建議</b>\n"
        f"{status_zh}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"<i>盤後 16:45 發送選股訊號</i>"
    )

    subs = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, msg)
    broadcast(msg, tier="free")
    broadcast(msg, tier="paid")
    logger.info("盤前集結建議已發送")

# ══════════════════════════════════════════════
# 盤後集結報告（16:45 台北時間）
# ══════════════════════════════════════════════
def send_daily_report(signals: List[Dict], market_overview: Dict, scan_stats: Dict):
    now_tw  = datetime.now(timezone(timedelta(hours=8)))
    idx     = market_overview.get("index", {})
    twii    = idx.get("twii", {});  vix = idx.get("vix", {})
    foreign = market_overview.get("foreign", {})
    score   = market_overview.get("sentiment_score", 50)

    buy_sigs  = [s for s in signals if s["direction"] == "buy"]
    sell_sigs = [s for s in signals if s["direction"] == "sell"]
    top3      = sorted(signals, key=lambda x: x["score"], reverse=True)[:3]

    twii_chg = twii.get("chg", 0)
    twii_str = (
        f"{twii.get('price',0):,.0f}點　"
        f"{'▲' if twii_chg>=0 else '▼'}{abs(twii_chg):.2f}%　"
        f"{'▲' if twii.get('chg_pt',0)>=0 else '▼'}{abs(twii.get('chg_pt',0)):,.0f}點"
    ) if twii else "—"

    fn     = foreign.get("net_buy_twd", 0) or 0
    fn_str = f"{'▲買超' if fn >= 0 else '▼賣超'} {abs(fn)/1e8:.1f}億"
    fn_em  = "💚" if fn >= 0 else "❤️"

    emoji_s = "🟢" if score >= 70 else "🟡" if score >= 50 else "🟠" if score >= 30 else "🔴"
    mood    = ("強烈看多" if score >= 80 else "偏多" if score >= 60 else
               "中性"     if score >= 40 else "偏空" if score >= 20 else "強烈看空")

    if twii_chg > 1.5:    comment = "大盤強勁，多頭氣氛濃厚"
    elif twii_chg > 0.3:  comment = "大盤小漲，偏多格局"
    elif twii_chg > -0.3: comment = "大盤盤整，方向未明"
    elif twii_chg > -1.5: comment = "大盤小跌，注意風險"
    else:                  comment = "大盤重挫，謹慎保守"

    tomorrow = _generate_tomorrow_plan(signals, market_overview)

    top3_str = "\n".join(
        f"  {i+1}. {'🔥' if s.get('grade')=='A' else '✅' if s.get('grade')=='B' else '👀'} "
        f"<b>{s['name']}（{s.get('code','')}）</b> "
        f"{'📈' if s['direction']=='buy' else '📉'} {s['score']}分\n"
        f"     進場 {s.get('entry_zone_low',0):.2f}~{s.get('entry_zone_high',0):.2f}｜"
        f"SL {s['stop_loss']:.2f}｜TP1 {s['tp1']:.2f}"
        for i, s in enumerate(top3)
    ) if top3 else "  今日無高分訊號"

    # ★ 修正：2026-09-19——使用者指示「統一為付費版」：盤後集結報告不再拆成
    # 「免費版摘要（msg_base，結尾寫『付費版查看完整分析』）」+「付費版完整清單
    # （msg_paid）」兩份，一律組成同一份完整報告（含完整訊號清單、完整明日
    # 計畫、DISCLAIMER），廣播給 free/paid 兩個名單跟 admin，內容完全一致。
    msg = (
        f"📊 <b>盤後集結報告</b> {now_tw.strftime('%m/%d')}\n"
        f"⏰ 台北時間 {now_tw.strftime('%H:%M')}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🇹🇼 <b>今日大盤</b>\n"
        f"加權指數：{twii_str}\n"
        f"VIX：{vix.get('price',0):.1f}\n"
        f"評語：{comment}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>今日法人動向</b>\n"
        f"{fn_em} 外資：{fn_str}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"{emoji_s} <b>市場情緒：{mood}</b>（{score}/100）\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>今日選股結果</b>\n"
        f"掃描　{scan_stats.get('scanned',0)} 檔\n"
        f"訊號　<b>{len(signals)} 個</b>　"
        f"📈 做多 {len(buy_sigs)}｜📉 做空 {len(sell_sigs)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"🏆 <b>今日最強訊號（前3）</b>\n"
        f"{top3_str}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>明日操作計畫</b>\n"
        f"{tomorrow['brief']}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"⏱ 耗時 {scan_stats.get('duration_min',0):.1f} 分鐘"
    )

    if signals:
        full_list = "\n".join(
            f"{i+1}. {'🔥' if s.get('grade')=='A' else '✅' if s.get('grade')=='B' else '👀'} "
            f"{s['name']}（{s.get('code','')}）"
            f"{'📈' if s['direction']=='buy' else '📉'} {s['score']}分\n"
            f"   進場 {s.get('entry_zone_low',0):.2f}~{s.get('entry_zone_high',0):.2f}｜"
            f"SL {s['stop_loss']:.2f}｜TP1 {s['tp1']:.2f}｜"
            f"建議{s.get('suggested_lots',1)}張"
            for i, s in enumerate(signals)
        )
        msg = (
            msg
            + f"\n━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>完整訊號清單</b>\n\n{full_list}\n\n"
            f"<b>明日完整計畫</b>\n{tomorrow['full']}\n\n"
            f"<i>{DISCLAIMER}</i>"
        )
    else:
        msg = msg + f"\n━━━━━━━━━━━━━━━━━\n<i>{DISCLAIMER}</i>"

    subs = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, msg)
    broadcast(msg, tier="free")
    broadcast(msg, tier="paid")
    logger.info(f"盤後集結報告已發送：{len(signals)} 個訊號")


def _generate_tomorrow_plan(signals: List[Dict], market_overview: Dict) -> Dict:
    score   = market_overview.get("sentiment_score", 50)
    status  = market_overview.get("market_status", "normal")
    fn      = market_overview.get("foreign", {}).get("net_buy_twd", 0) or 0
    buy_cnt = len([s for s in signals if s["direction"] == "buy"])
    # ★ 修正：2026-09-19——稽核發現明日計畫的 full 文字只統計 buy_cnt、完全沒提
    # 做空機會數，即使未來 ENABLE_SHORT_SIGNALS 重啟、signals 裡真的出現 sell
    # 訊號，這段文字也只會顯示「N 個做多機會」，讓使用者誤以為系統沒抓到放空
    # 機會。目前 ENABLE_SHORT_SIGNALS=False，所以現階段 sell_cnt 必為0、無實際
    # 影響，但這裡先補上，避免未來重啟做空後又出現一個新的「介面沒同步」問題。
    sell_cnt = len([s for s in signals if s["direction"] == "sell"])
    top_sig = max(signals, key=lambda x: x["score"], default=None) if signals else None

    if score >= 65 and fn >= 0 and status == "normal":
        direction = "偏多操作"
        strategy  = "可積極建立多頭部位，量能放大才追"
    elif score >= 50:
        direction = "中性觀察"
        strategy  = "選強勢股分批進場，嚴格執行停損"
    else:
        direction = "偏空謹慎"
        strategy  = "以觀望為主，避免逆勢操作"

    if top_sig:
        brief = (
            f"方向：{direction}\n"
            f"重點：{top_sig['name']}（{top_sig.get('code','')}）{top_sig['score']}分\n"
            f"注意進場區 {top_sig.get('entry_zone_low',0):.2f}~{top_sig.get('entry_zone_high',0):.2f}\n"
            f"策略：{strategy}"
        )
        full = (
            f"整體方向：{direction}\n"
            f"市場情緒：{score}/100\n"
            f"外資：{'買超' if fn>=0 else '賣超'}{abs(fn)/1e8:.1f}億\n\n"
            f"今日 {buy_cnt} 個做多機會" + (f"、{sell_cnt} 個做空機會" if sell_cnt else "") + "\n"
            f"策略：{strategy}\n\n"
            f"重點標的：\n"
            + "\n".join(
                f"  ▪ {s['name']}（{s.get('code','')}）"
                f"進場 {s.get('entry_zone_low',0):.2f}~{s.get('entry_zone_high',0):.2f}｜"
                f"SL {s['stop_loss']:.2f}"
                for s in signals[:5]
            )
        )
    else:
        brief = f"方向：{direction}\n今日無明確訊號，明日持續觀察\n{strategy}"
        full  = brief

    return {"brief": brief, "full": full}

# ══════════════════════════════════════════════
# 推播個別訊號
# ══════════════════════════════════════════════
def push_signal(sig: Dict):
    paid_msg = format_signal_paid(sig)

    if TELEGRAM_FREE_CHANNEL:
        send_message(TELEGRAM_FREE_CHANNEL, paid_msg)
    else:
        broadcast(paid_msg, tier="free")
    time.sleep(0.3)
    if TELEGRAM_PAID_CHANNEL:
        send_message(TELEGRAM_PAID_CHANNEL, paid_msg)
    else:
        broadcast(paid_msg, tier="paid")

    subs = _load_subscribers()
    admin_ids = subs.get("admin", [])
    admin_ok  = sum(1 for admin_id in admin_ids if send_message(admin_id, paid_msg))
    # ★ 修正：2026-09-03——把 admin 直送的成功/失敗數量記進 log，這樣機主
    # 之後只要看 Render log 就能確認每一筆訊號到底有沒有真的送到自己手機，
    # 不用再靠猜的。
    logger.info(
        f"訊號推播：{sig.get('name','')} {sig.get('direction','')} score={sig['score']} "
        f"admin {admin_ok}/{len(admin_ids)}"
    )

# ══════════════════════════════════════════════
# 盤中持倉現況總覽（12:00 台北時間，見 scanner.check_intraday_price_alerts）
# ══════════════════════════════════════════════
def send_intraday_digest(pending: List[Dict], prices: Dict[str, float]):
    """★ 新增：2026-09-18——使用者反映盤中「請留意止損位」的制式提醒之外，
    整天看不到任何實際數字，只有真的觸及SL/TP才會收到訊息，感覺像系統整天
    沒在動。這裡固定每天中午12:00送一次持倉現況總覽，讓現價、以及現價落在
    停損/停利區間的相對位置實際被看見，而不是只被告知「請留意」卻沒有任何
    可以參考的數字。抓不到現價的標的仍會列出、標示「—」，不會整筆略過造成
    使用者誤以為那筆訊號不存在了。"""
    if not pending:
        return
    lines = []
    for s in pending[:10]:
        ticker    = s.get("ticker")
        price     = prices.get(ticker)
        sl, tp1   = s.get("stop_loss"), s.get("tp1")
        direction = s.get("direction")
        icon      = "📈" if direction == "buy" else "📉"
        if price and sl and tp1 and (sl != tp1):
            # 用「現價在 SL→TP1 這段區間裡走了多遠」粗略表示風險位置，
            # 0% = 剛好在停損、100% = 剛好在TP1，僅供參考，不是精確風控指標。
            rng = tp1 - sl
            pos_pct = (price - sl) / rng * 100
            pos_pct = max(0, min(100, pos_pct))
            price_str = f"現價 {price:.2f}（距停損/停利區間 {pos_pct:.0f}%）"
        elif price:
            price_str = f"現價 {price:.2f}"
        else:
            price_str = "現價暫時抓不到（—）"
        lines.append(
            f"  ▪ {s.get('name','')}（{s.get('code','')}）{icon}\n"
            f"     {price_str}｜SL {sl:.2f}｜TP1 {tp1:.2f}" if sl and tp1 else
            f"  ▪ {s.get('name','')}（{s.get('code','')}）{icon} {price_str}"
        )
    msg = (
        "📍 <b>盤中持倉現況</b>（12:00）\n"
        + "\n".join(lines) + "\n"
        "<i>現價來源：yfinance，可能有延遲，僅供參考</i>"
    )
    subs = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, msg)
    broadcast(msg, tier="free")
    broadcast(msg, tier="paid")
    logger.info("盤中持倉現況總覽已發送")

# ══════════════════════════════════════════════
# 系統警報
# ══════════════════════════════════════════════
def send_alert(message: str, level: str = "info"):
    emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}.get(level, "📢")
    subs  = _load_subscribers()
    for admin_id in subs.get("admin", []):
        send_message(admin_id, f"{emoji} <b>系統通知</b>\n{message}")

# ══════════════════════════════════════════════
# Bot 指令
# ══════════════════════════════════════════════
def handle_update(update: Dict) -> Optional[str]:
    msg     = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip()
    if not text or not chat_id:
        return None
    cmd = text.split()[0].lower()

    if cmd == "/start":
        add_subscriber(chat_id, "free")
        # ★ 修正：2026-09-19——使用者指示「統一為付費版」後，所有訂閱者收到的
        # 訊號內容已經完全一致（TP1/TP2/TP3、建議張數、法人動向都有），不再有
        # 「加購解鎖」這件事，所以歡迎詞跟 /upgrade 一併移除舊的升級推銷文字，
        # 避免文字承諾（有東西可以升級）跟實際行為（大家收到的內容都一樣）
        # 不一致，誤導使用者。
        return (
            f"👋 歡迎！<b>{SYSTEM['name']}</b>\n\n"
            f"📅 <b>推播時間</b>\n"
            f"🌅 08:45 盤前集結建議\n"
            f"📊 16:45 盤後選股報告（完整訊號清單）\n\n"
            f"📊 <b>訊號等級</b>\n"
            f"🔥 A級（85+）強力建議\n"
            f"✅ B級（75+）良好訊號\n"
            f"👀 C級（65+）觀察機會\n\n"
            f"/fill 代號 價格 — 回報實際成交價（例：/fill 2330 985.5）\n"
            f"/help — 使用說明\n\n"
            f"<i>{DISCLAIMER}</i>"
        )
    elif cmd == "/stop":
        remove_subscriber(chat_id)
        return "已取消訂閱。感謝使用！"
    elif cmd == "/fill":
        # ★ 新增：2026-09-16——回應三方AI交叉比對中ChatGPT提出的建議：讓使用者
        # 可以回報實際成交價，用來累積「訊號參考價 vs 實際成交價」的真實滑價
        # 資料（見 state_store.py actual_entry_price/get_slippage_stats() 說明），
        # 而不是像現在只能在 DISCLAIMER 裡誠實承認「不知道滑價多少」。
        parts = text.split()
        if len(parts) < 3:
            return "用法：/fill 股票代號 實際成交價\n例如：/fill 2330 985.5\n（會記錄在你目前該檔最新一筆未平倉訊號上，用來幫助未來校正系統的滑價估計）"
        code_arg = parts[1].strip()
        try:
            actual_price = float(parts[2])
        except ValueError:
            return "價格格式錯誤，請輸入數字，例如：/fill 2330 985.5"
        if actual_price <= 0:
            return "價格必須大於 0。"
        from state_store import store
        pending = store.get_pending_signals()
        matches = [s for s in pending if s.get("code") == code_arg or str(s.get("ticker", "")).startswith(code_arg)]
        if not matches:
            return f"找不到代號 {code_arg} 目前有效（未平倉）的訊號，請確認代號是否正確。"
        matches.sort(key=lambda s: s.get("generated_at", ""), reverse=True)
        sig = matches[0]
        if not store.record_actual_fill(sig["id"], actual_price):
            return "記錄失敗，請稍後再試。"
        ref_price = sig.get("entry_price") or sig.get("current_price") or 0
        slip_txt = ""
        if ref_price:
            sign = 1 if sig.get("direction") == "buy" else -1
            slip_pct = round(sign * (actual_price - ref_price) / ref_price * 100, 2)
            slip_txt = f"（與訊號參考價 {ref_price} 相比，滑價 {slip_pct:+.2f}%）"
        return f"✅ 已記錄 {sig.get('name', '')} {code_arg} 實際成交價 {actual_price}{slip_txt}\n感謝回報，這筆資料會用於未來校正系統的滑價估計。"
    elif cmd == "/help":
        return (
            f"📖 <b>使用說明</b>\n\n"
            f"每日掃描 1000+ 台股，波段策略選股。\n\n"
            f"🌅 08:45 盤前集結建議\n"
            f"美股昨收｜外資動向｜持倉追蹤｜今日計畫\n\n"
            f"📊 16:45 盤後選股報告\n"
            f"今日大盤｜法人動向｜選股訊號｜明日計畫\n\n"
            f"💡 <b>/fill 代號 價格</b>\n"
            f"收到訊號後，如果你有實際下單，可以回報實際成交價（例：/fill 2330 985.5），"
            f"幫助我們累積真實滑價資料，未來讓績效統計更貼近你的實際結果。\n\n"
            f"<i>{DISCLAIMER}</i>"
        )
    return None

def set_webhook(webhook_url: str) -> bool:
    try:
        r = requests.post(
            f"{BASE_URL}/setWebhook",
            json={"url": webhook_url},
            timeout=15,
        )
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
        if r.status_code == 200:
            return r.json().get("result", {})
    except:
        pass
    return {}
