"""資料新鮮度：每一種市場資料「現在拿到的是哪一天」與「現在應該要拿到哪一天」。

兩個用途：
1. /api/freshness 給網頁顯示「資料更新狀態」，並讓網頁偵測到新資料時自動重新載入；
2. job_poll()：收盤後的公布時段（平日 14:30～22:00）每 10 分鐘檢查一次，哪一種資料還沒更新到
   應有的日期就只重抓那一種，抓到就停（不會一直重打官方 API）。

「應有日期」＝今天（若今天是交易日且已過該資料的官方公布時間），否則為上一個交易日。
這裡只讀記憶體快取與資料庫，不打外部 API；真正的重抓只在 job_poll() 裡做。
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# (key, 名稱, 來源, 官方大約公布時間 HH:MM)
DATASETS = [
    ("intraday", "盤中／收盤即時報價", "證交所 MIS（每 5 分鐘全市場快照）", "09:05"),
    ("otc_quote", "上櫃日行情", "櫃買中心 OpenAPI tpex_mainboard_quotes", "15:30"),
    ("tse_quote", "上市日行情", "證交所 OpenAPI STOCK_DAY_ALL", "18:00"),
    ("foreign", "外資買賣超金額", "證交所 BFI82U", "15:30"),
    ("inst", "三大法人個股買賣超", "證交所 T86", "16:30"),
    ("taifex", "期交所三大法人期貨／選擇權", "期交所 OpenAPI（網站 CSV 備援）", "15:30"),
    ("margin", "融資餘額", "證交所 MI_MARGN", "21:30"),
    ("scan", "每日訊號掃描", "本系統（16:30 盤後掃描）", "17:30"),
]


def now_tpe() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _is_holiday(d: datetime) -> bool:
    try:
        from data_fetcher import _is_tw_market_holiday
        return bool(_is_tw_market_holiday(d))
    except Exception:
        return False


def is_trading_day(d: datetime, holiday: Callable[[datetime], bool] = _is_holiday) -> bool:
    return d.weekday() < 5 and not holiday(d)


def expected_date(publish_hm: str, now: Optional[datetime] = None,
                  holiday: Callable[[datetime], bool] = _is_holiday) -> str:
    """這份資料「現在應該已經有」的最新交易日（YYYY-MM-DD）。"""
    now = now or now_tpe()
    h, m = (int(x) for x in publish_hm.split(":"))
    d = now
    if not (is_trading_day(d, holiday) and (now.hour, now.minute) >= (h, m)):
        d = d - timedelta(days=1)
        for _ in range(15):
            if is_trading_day(d, holiday):
                break
            d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _iso(s) -> Optional[str]:
    s = str(s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10] if len(s) >= 10 and s[4] == "-" else None


def current_dates() -> Dict[str, Optional[str]]:
    """各資料目前拿到的日期（只讀快取／資料庫）。"""
    out: Dict[str, Optional[str]] = {}
    try:
        from state_store import store
    except Exception:
        store = None
    try:
        snap = store.get_meta("intraday_snapshot") or {} if store else {}
        out["intraday"] = max(((v or {}).get("date") or "" for v in (snap.get("indices") or {}).values()), default="") or None
    except Exception:
        out["intraday"] = None
    try:
        import stock_universe as su
        raw = su._raw_mem_cache or su._load_raw_from_db() or {}
        out["tse_quote"], out["otc_quote"] = raw.get("tse_quote_date"), raw.get("otc_quote_date")
    except Exception:
        out["tse_quote"] = out["otc_quote"] = None
    try:
        import data_fetcher as df
        ent = df._cache.get("foreign_total")
        out["foreign"] = _iso((ent or {}).get("data", {}).get("date")) if ent else None
    except Exception:
        out["foreign"] = None
    try:
        out["inst"] = (store.get_inst_dates(limit=1) or [None])[0] if store else None
    except Exception:
        out["inst"] = None
    try:
        import taifex
        v = taifex._cache.get("v") or {}
        d = (v.get("futures") or {}).get("date")
        if not d and store:
            d = ((store.get_meta("taifex_last") or {}).get("futures") or {}).get("date")
        out["taifex"] = d
    except Exception:
        out["taifex"] = None
    try:
        hist = store.get_meta("margin_balance_history", []) if store else []
        out["margin"] = _iso(hist[-1].get("date")) if hist else None
    except Exception:
        out["margin"] = None
    try:
        from scanner import scanner
        at = scanner.get_status().get("last_scan_at")
        if at:
            t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            if t.tzinfo:
                t = t.astimezone(timezone.utc) + timedelta(hours=8)
            out["scan"] = t.strftime("%Y-%m-%d")
        else:
            out["scan"] = None
    except Exception:
        out["scan"] = None
    return out


def status(now: Optional[datetime] = None, dates: Optional[Dict[str, Optional[str]]] = None) -> Dict:
    now = now or now_tpe()
    dates = dates if dates is not None else current_dates()
    items: List[Dict] = []
    for key, name, src, hm in DATASETS:
        exp = expected_date(hm, now)
        d = dates.get(key)
        st = "unknown" if not d else ("ok" if d >= exp else "late")
        items.append({"key": key, "name": name, "source": src, "publish": hm, "date": d, "expected": exp, "status": st})
    return {"at": time.time(), "now": now.strftime("%Y-%m-%d %H:%M"), "items": items,
            "version": "|".join(f"{i['key']}={i['date']}" for i in items),
            "late": [i["key"] for i in items if i["status"] == "late"]}


def job_poll() -> Dict:
    """公布時段每 10 分鐘：只重抓還沒到應有日期的那幾種資料。"""
    now = now_tpe()
    st = {i["key"]: i for i in status(now)["items"]}
    did = []

    def late(k):
        return st.get(k, {}).get("status") in ("late", "unknown")

    try:
        if late("taifex"):
            import taifex
            from state_store import store
            taifex.update_hist(store, taifex.get_taifex(force=True))
            did.append("taifex")
    except Exception as e:
        logger.warning(f"freshness taifex: {e}")
    try:
        if late("inst"):
            from data_fetcher import collect_inst_daily
            from state_store import store
            n = collect_inst_daily(st["inst"]["expected"].replace("-", ""), store)
            did.append(f"inst({n})")
            if n:
                import data_fetcher as df
                df._cache_clear("inst_today")
    except Exception as e:
        logger.warning(f"freshness inst: {e}")
    try:
        if late("tse_quote") or late("otc_quote"):
            from stock_universe import refresh_if_date_stale
            if refresh_if_date_stale():
                did.append("universe")
    except Exception as e:
        logger.warning(f"freshness universe: {e}")
    try:
        if late("foreign"):
            import data_fetcher as df
            df._cache_clear("foreign_total"); df._cache_clear("market_overview")
            df.fetch_foreign_total_flow()
            did.append("foreign")
    except Exception as e:
        logger.warning(f"freshness foreign: {e}")
    try:
        if late("margin"):
            import data_fetcher as df
            df._cache_clear("margin_change"); df._cache_clear("margin_change_fail"); df._cache_clear("market_overview")
            df.fetch_margin_change()
            did.append("margin")
    except Exception as e:
        logger.warning(f"freshness margin: {e}")
    if did:
        try:
            import protect
            protect.clear_cache()
        except Exception:
            pass
        logger.info(f"資料新鮮度補抓：{did}")
    return {"did": did}
