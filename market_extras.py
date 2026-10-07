"""證交所補充資料：交易限制旗標、借券／融券餘額、當沖標的、可借券股數。

來源（全為證交所官方，欄位 2026-10-07 以瀏覽器實測）：
  OpenAPI exchangeReport/TWTAWU   暫停交易證券（Code, TradingHaltDate/Time, TradingResumptionDate/Time；民國年 YYYMMDD）
  OpenAPI exchangeReport/TWT85U   證券變更交易（分盤交易／全額交割類，Code）
  OpenAPI exchangeReport/BFI84U   停資停券預告（Code, StartDate, EndDate, Reason）
  OpenAPI exchangeReport/TWT88U   新上市首五日無漲跌幅（SecurCode, 5thTradingDate）
  OpenAPI exchangeReport/TWTB4U   當日沖銷標的（Code, Suspension=Y 表暫停先賣後買）
  OpenAPI SBL/TWT96U              當日可借券賣出股數（TWSECode/GRETAICode，含上櫃）
  RWD marginTrading/TWT93U        融券＋借券賣出餘額（逐日，可帶 date）
上櫃（TPEx OpenAPI，欄位 2026-10-07 以瀏覽器實測）：
  tpex_disposal_information       處置有價證券（SecuritiesCompanyCode, DispositionPeriod, DispositionReasons；民國年 YYYMMDD）
  tpex_trading_warning_information 注意股票（SecuritiesCompanyCode, TradingInformation）
  tpex_cmode                      變更交易／分盤／管理股票／停止交易（AlteredTrading, PeriodicTrading, ManagedStock, SuspensionOfTrading；值為全形「Ｙ」）
  tpex_spendi_today               當日公布暫停／恢復交易
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OPENAPI = "https://openapi.twse.com.tw/v1/"
TPEX = "https://www.tpex.org.tw/openapi/v1/"
TWT93U = "https://www.twse.com.tw/rwd/zh/marginTrading/TWT93U?response=json&date={d}"

_TTL = 900
_cache = {"ts": 0.0, "v": None}
_lock = threading.Lock()
_status = {"last_ok": None, "last_error": None, "counts": {}}


def _roc(s) -> Optional[str]:
    """'1150813' → '2026-08-13'；格式不對回傳 None。"""
    s = re.sub(r"\D", "", str(s or ""))
    if len(s) != 7:
        return None
    return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"


def _tm(s) -> str:
    s = re.sub(r"\D", "", str(s or "")).ljust(6, "0")[:6]
    return f"{s[:2]}:{s[2:4]}:{s[4:6]}"


def _num(x) -> Optional[int]:
    try:
        return int(float(str(x).replace(",", "").strip()))
    except Exception:
        return None


def _get(url: str, timeout: int = 20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def _now_tpe() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def build_flags(now: Optional[datetime] = None) -> Dict:
    """抓所有旗標類資料；單一來源失敗不影響其他，失敗項目記錄在 errors。"""
    now = now or _now_tpe()
    today = now.strftime("%Y-%m-%d")
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    out = {"date": today, "at": time.time(), "halt": {}, "altered": [], "margin_stop": {},
           "no_limit": [], "daytrade_ok": [], "daytrade_suspended": [], "sbl_avail": {},
           "otc_disposal": {}, "otc_attention": {}, "errors": []}

    def run(name, fn):
        try:
            fn()
        except Exception as e:
            out["errors"].append(f"{name}: {e}")
            logger.warning(f"market_extras {name}: {e}")

    def halts():
        for r in _get(OPENAPI + "exchangeReport/TWTAWU"):
            code = str(r.get("Code", "")).strip()
            hd, rd = _roc(r.get("TradingHaltDate")), _roc(r.get("TradingResumptionDate"))
            if not code or not hd:
                continue
            start = f"{hd} {_tm(r.get('TradingHaltTime'))}"
            end = f"{rd} {_tm(r.get('TradingResumptionTime'))}" if rd else None
            if start <= now_s and (end is None or end > now_s):
                out["halt"][code] = {"from": start, "to": end}

    def altered():
        out["altered"] = sorted({str(r.get("Code", "")).strip() for r in _get(OPENAPI + "exchangeReport/TWT85U") if r.get("Code")})

    def mstop():
        for r in _get(OPENAPI + "exchangeReport/BFI84U"):
            code, s, e = str(r.get("Code", "")).strip(), _roc(r.get("StartDate")), _roc(r.get("EndDate"))
            if code and s and e and s <= today <= e:
                out["margin_stop"][code] = {"from": s, "to": e, "reason": r.get("Reason", "")}

    def nolimit():
        out["no_limit"] = sorted({str(r.get("SecurCode", "")).strip() for r in _get(OPENAPI + "exchangeReport/TWT88U")
                                  if r.get("SecurCode") and (_roc(r.get("5thTradingDate")) or "0") >= today})

    def daytrade():
        ok, sus = [], []
        for r in _get(OPENAPI + "exchangeReport/TWTB4U"):
            c = str(r.get("Code", "")).strip()
            if c:
                ok.append(c)
                if str(r.get("Suspension", "")).strip().upper() == "Y":
                    sus.append(c)
        out["daytrade_ok"], out["daytrade_suspended"] = sorted(ok), sorted(sus)

    def sbl():
        for r in _get(OPENAPI + "SBL/TWT96U"):
            for ck, vk in (("TWSECode", "TWSEAvailableVolume"), ("GRETAICode", "GRETAIAvailableVolume")):
                c, v = str(r.get(ck, "")).strip(), _num(r.get(vk))
                if c and v is not None:
                    out["sbl_avail"][c] = v

    def _yes(v) -> bool:
        return str(v or "").strip() in ("Y", "y", "Ｙ")

    def otc_watch():
        for r in _get(TPEX + "tpex_disposal_information"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            if c:
                out["otc_disposal"][c] = {"name": str(r.get("CompanyName", "")).strip(),
                                          "period": str(r.get("DispositionPeriod", "")).strip(),
                                          "reason": str(r.get("DispositionReasons", "")).strip()[:80]}
        for r in _get(TPEX + "tpex_trading_warning_information"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            if c:
                out["otc_attention"][c] = {"name": str(r.get("CompanyName", "")).strip(),
                                           "period": _roc(r.get("Date")) or "",
                                           "reason": str(r.get("TradingInformation", "")).strip()[:80]}

    def otc_cmode():
        alt = set(out["altered"])
        for r in _get(TPEX + "tpex_cmode"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            if not c:
                continue
            if _yes(r.get("SuspensionOfTrading")):
                out["halt"].setdefault(c, {"from": _roc(r.get("Date")) or today, "to": None})
            if any(_yes(r.get(k)) for k in ("AlteredTrading", "PeriodicTrading", "ManagedStock")):
                alt.add(c)
        out["altered"] = sorted(alt)

    def otc_halt_today():
        for r in _get(TPEX + "tpex_spendi_today"):
            c = str(r.get("SecuritiesCompanyCode", "")).strip()
            if c and str(r.get("暫停交易", "")).strip() and not str(r.get("恢復交易", "")).strip():
                out["halt"].setdefault(c, {"from": today, "to": None})

    for n, f in (("暫停交易", halts), ("變更交易", altered), ("停資停券", mstop), ("首五日無漲跌幅", nolimit),
                 ("當沖標的", daytrade), ("可借券", sbl),
                 ("上櫃處置注意", otc_watch), ("上櫃變更交易", otc_cmode), ("上櫃暫停交易", otc_halt_today)):
        run(n, f)
    return out


def get_flags(force: bool = False) -> Dict:
    """15 分鐘記憶體快取；整批失敗時沿用舊資料（不回空，避免誤放行）。"""
    with _lock:
        if not force and _cache["v"] and time.time() - _cache["ts"] < _TTL:
            return _cache["v"]
    try:
        v = build_flags()
        n_ok = 9 - len(v["errors"])
        if n_ok <= 0 and _cache["v"]:
            _status["last_error"] = "; ".join(v["errors"])[:200]
            return _cache["v"]
        with _lock:
            _cache["v"], _cache["ts"] = v, time.time()
        _status["last_ok"] = time.time()
        _status["last_error"] = "; ".join(v["errors"])[:200] or None
        _status["counts"] = {"halt": len(v["halt"]), "altered": len(v["altered"]), "margin_stop": len(v["margin_stop"]),
                             "no_limit": len(v["no_limit"]), "daytrade_ok": len(v["daytrade_ok"]),
                             "sbl_avail": len(v["sbl_avail"]),
                             "otc_disposal": len(v["otc_disposal"]), "otc_attention": len(v["otc_attention"])}
        return v
    except Exception as e:
        _status["last_error"] = str(e)
        return _cache["v"] or {"date": None, "halt": {}, "altered": [], "margin_stop": {}, "no_limit": [],
                               "daytrade_ok": [], "daytrade_suspended": [], "sbl_avail": {},
                               "otc_disposal": {}, "otc_attention": {}, "errors": [str(e)]}


def hard_exclude_codes() -> set:
    """買不到或流動性極差、不該出訊號的標的：目前暫停交易中、分盤／變更交易（含上櫃）。"""
    f = get_flags()
    return set(f.get("halt", {}).keys()) | set(f.get("altered", []))


def otc_watch_codes() -> set:
    """上櫃處置＋注意股代號（與上市處置／注意股同樣排除，不出訊號）。"""
    f = get_flags()
    return set(f.get("otc_disposal", {}).keys()) | set(f.get("otc_attention", {}).keys())


def status() -> Dict:
    return dict(_status)


# ───────── 融券＋借券賣出餘額（逐日歷史，供 5 日變化） ─────────
def parse_twt93u(j: Dict) -> Dict[str, List[int]]:
    """→ {code: [融券今日餘額, 借券賣出今日餘額, 融券前日餘額, 借券前日餘額]}（單位：股）。"""
    out = {}
    if not j or j.get("stat") != "OK":
        return out
    for r in j.get("data") or []:
        if len(r) < 14:
            continue
        code = str(r[0]).strip()
        ms, ss, msp, ssp = _num(r[6]), _num(r[12]), _num(r[2]), _num(r[8])
        if code and ms is not None and ss is not None:
            out[code] = [ms, ss, msp or 0, ssp or 0]
    return out


def update_short_hist(store, max_days: int = 8) -> Dict:
    """補齊最近交易日的融券／借券賣出餘額，存在 meta short_hist（保留 12 個交易日）。"""
    hist = store.get_meta("short_hist") or {}
    added = []
    d = _now_tpe()
    for _ in range(max_days + 6):
        if d.weekday() < 5:
            iso = d.strftime("%Y-%m-%d")
            if iso not in hist:
                try:
                    j = _get(TWT93U.format(d=d.strftime("%Y%m%d")), timeout=25)
                    rows = parse_twt93u(j)
                    if len(rows) > 500:
                        hist[iso] = rows
                        added.append(iso)
                except Exception as e:
                    logger.warning(f"update_short_hist {iso}: {e}")
        d -= timedelta(days=1)
    for k in sorted(hist.keys())[:-12]:
        hist.pop(k, None)
    if added:
        store.set_meta("short_hist", hist)
        logger.info(f"融券／借券賣出餘額歷史已更新：新增 {added}，共 {len(hist)} 日")
    return {"added": added, "days": sorted(hist.keys())}


def short_summary(hist: Dict, code: str, volume_lots: Optional[float] = None) -> Optional[Dict]:
    """個股融券／借券賣出餘額（張）、5 日變化、占成交量比。資料不足回傳 None。"""
    days = sorted(d for d in hist if code in hist[d])
    if not days:
        return None
    last = hist[days[-1]][code]
    base = hist[days[-6]][code] if len(days) >= 6 else (hist[days[0]][code] if len(days) >= 2 else None)
    s = {"date": days[-1],
         "margin_short_lots": round(last[0] / 1000, 1), "sbl_short_lots": round(last[1] / 1000, 1),
         "total_short_lots": round((last[0] + last[1]) / 1000, 1)}
    if base is not None:
        s["margin_short_chg_lots"] = round((last[0] - base[0]) / 1000, 1)
        s["sbl_short_chg_lots"] = round((last[1] - base[1]) / 1000, 1)
        s["chg_days"] = len(days) - 1 if len(days) < 6 else 5
    if volume_lots and volume_lots > 0:
        s["short_vs_volume_days"] = round(s["total_short_lots"] / volume_lots, 2)   # 空單餘額相當於幾天的成交量
    return s


def stock_flags(code: str, volume_lots: Optional[float] = None, store=None) -> Dict:
    """單檔彙整：給研究頁、健康檢查、訊號備註共用。"""
    f = get_flags()
    out = {"code": code, "flags": [], "data_date": f.get("date"), "errors": f.get("errors", [])}
    if code in f.get("halt", {}):
        h = f["halt"][code]
        out["flags"].append({"key": "halt", "level": "fail", "text": f"暫停交易中（{h['from']} 起，預計恢復 {h['to'] or '未定'}）"})
    if code in f.get("altered", []):
        out["flags"].append({"key": "altered", "level": "fail", "text": "變更交易（分盤交易／全額交割類），流動性與成交風險高"})
    if code in f.get("margin_stop", {}):
        m = f["margin_stop"][code]
        out["flags"].append({"key": "margin_stop", "level": "warn", "text": f"停資停券 {m['from']}～{m['to']}（{m['reason']}）"})
    if code in f.get("no_limit", []):
        out["flags"].append({"key": "no_limit", "level": "warn", "text": "新上市首五日無漲跌幅限制"})
    if code in f.get("daytrade_suspended", []):
        out["flags"].append({"key": "daytrade_suspended", "level": "info", "text": "暫停先賣後買當日沖銷"})
    out["daytrade_ok"] = code in f.get("daytrade_ok", [])
    if code in f.get("sbl_avail", {}):
        out["sbl_avail_lots"] = round(f["sbl_avail"][code] / 1000, 1)
    try:
        if store is not None:
            out["short"] = short_summary(store.get_meta("short_hist") or {}, code, volume_lots)
    except Exception as e:
        out["short_error"] = str(e)
    return out
