"""事件濾網（2026-11-04）：把「重大訊息風險」與「近日除權息」接進訊號產生，自動影響訊號。

設計：
- 這兩類事件的名單由既有來源建立（fundamentals.fetch_material_news_risk_map 已過濾風險關鍵字、
  calendar_events.get_ex_dividend 官方預告），本模組只做「每次掃描建一次代號集合、全程重用」的
  TTL 快取，避免 generate_signal_tw 每檔都重抓。
- fail-soft：來源抓取失敗時沿用上一份、否則回空集合——寧可「這次沒擋到」也不要因為抓不到事件
  資料就把所有訊號都擋掉（事件濾網是加分保護，不是唯一風控）。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))
_cache = {}
_TTL = 600          # 10 分鐘：一次全市場掃描期間共用同一份名單


def _today_iso() -> str:
    return datetime.now(_TPE).strftime("%Y-%m-%d")


def _get(key, builder):
    now = time.time()
    c = _cache.get(key)
    if c and now - c[0] < _TTL:
        return c[1]
    try:
        v = builder()
    except Exception as e:
        logger.warning(f"event_filter {key}: {e}")
        v = c[1] if c else set()        # fail-soft：沿用上一份或回空，不因抓取失敗而擋掉所有訊號
    _cache[key] = (now, v)
    return v


def imminent_exdiv_codes(within_days: int = 2) -> set:
    """今天起 within_days 個日曆日內除權息的代號集合。"""
    def build():
        import calendar_events
        d = calendar_events.get_ex_dividend(days=within_days + 1) or {}
        lo = _today_iso()
        hi = (datetime.now(_TPE) + timedelta(days=within_days)).strftime("%Y-%m-%d")
        return {str(x.get("code")) for x in (d.get("items") or [])
                if x.get("code") and lo <= str(x.get("date") or "") <= hi}
    return _get(f"exdiv_{within_days}", build)


def material_news_codes() -> set:
    """命中重大訊息風險關鍵字的代號集合（僅上市涵蓋）。"""
    def build():
        from fundamentals import fetch_material_news_risk_map
        m = fetch_material_news_risk_map() or {}
        return {str(k) for k, v in m.items() if v}
    return _get("news", build)


def check(code, within_days: int = 2) -> dict:
    """回傳某代號目前的事件風險：{exdiv, news}。"""
    c = str(code)
    return {"exdiv": c in imminent_exdiv_codes(within_days), "news": c in material_news_codes()}
