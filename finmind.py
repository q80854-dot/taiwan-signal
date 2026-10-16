"""FinMind 財報整合（選用）：補齊個股研究「財務指標」的完整季度歷史——
每股盈餘(EPS)、毛利率、營業利益率、淨利率、ROE(淨資產收益率)、ROA(總資產收益率)、
流動比率、股東權益比率。

啟用條件（兩者皆需，否則自動略過、不影響其他功能）：
1. 環境變數 FINMIND_TOKEN（到 finmindtrade.com 免費註冊取得；無 token 時請求額度較低）。
2. Render 網路政策允許連 api.finmindtrade.com。

安全／正確性設計：
- 無 token → 直接回空（不打外部、不報錯）；任何例外都優雅降級。
- 綜合損益表（損益類）官方為「累計數（YTD）」，本模組會依會計年度還原「單季」數字
  （Q1 取原值，其後 = 本季累計 − 上一季累計）；資產負債表為「時點數」不還原。
- 每個指標各自獨立計算：對應科目找不到就「該指標留空」，絕不輸出推估或錯誤數字。
- 欄位用別名（英文＋中文）比對，容忍 FinMind/官方欄名差異。
"""
import logging
import os
import threading
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
_TTL = 24 * 3600
_FALLBACK_TTL = 3600          # 以 DB 回退的資料只在記憶體快取 1 小時，額度一恢復就會重新抓最新
_cache: Dict[str, dict] = {}
_lock = threading.Lock()

# ── 額度熔斷（2026-10-16）──────────────────────────────────────────────
# FinMind 免費額度盤中很容易用完，回 HTTP 402「Requests reach the upper limit」。
# 一旦打爆還繼續狂打，只會把封鎖拉得更長，且每次請求都卡滿逾時（個股頁曾因此慢到 22 秒）。
# 作法：看到額度上限就開啟熔斷，冷卻期間所有 FinMind 呼叫直接略過（不連線），
# 高階函式改回退資料庫最近一次的正確結果（季報/法人本就不是盤中會變的資料）。
_cooldown_until = 0.0
_COOLDOWN = 900               # 15 分鐘


def circuit_open() -> bool:
    """目前是否因額度上限而熔斷中。"""
    return time.time() < _cooldown_until


def _trip(secs: Optional[int] = None):
    global _cooldown_until
    _cooldown_until = time.time() + (secs or _COOLDOWN)
    logger.warning(f"finmind：已達額度上限，熔斷 {int((secs or _COOLDOWN) / 60)} 分鐘，期間改用資料庫快取或略過")


def _db_get(key: str):
    try:
        from state_store import store
        return store.get_meta(key)
    except Exception:
        return None


def _db_put(key: str, data):
    try:
        from state_store import store
        store.set_meta(key, {"at": time.time(), "data": data})
    except Exception as e:
        logger.warning(f"finmind _db_put({key}): {e}")


def _cache_fresh(ent) -> bool:
    """記憶體快取是否仍新鮮：正常資料用 _TTL，DB 回退資料用較短的 _FALLBACK_TTL（好讓額度恢復後儘快重抓）。"""
    if not ent:
        return False
    ttl = _FALLBACK_TTL if ent.get("fallback") else _TTL
    return time.time() - ent["at"] < ttl

# 科目別名（任一子字串命中即算；exclude 用來排除容易誤抓的近似科目）
_ALIAS = {
    "revenue":   (["Revenue", "營業收入", "收入合計", "收益合計"], ["Cost", "成本", "OtherIncome", "業外", "其他"]),
    "gross":     (["GrossProfit", "營業毛利"], ["率", "margin"]),
    "op_income": (["OperatingIncome", "營業利益"], ["率", "margin", "外"]),
    "net_income":(["IncomeAfterTaxes", "ProfitForThePeriod", "本期淨利", "稅後淨利", "本期net"], ["非控制", "其他綜合", "綜合損益總額", "率", "每股"]),
    "eps":       (["EPS", "每股盈餘", "每股盈虧"], ["稀釋", "Diluted"]),
    "equity":    (["EquityAttributableToOwnersOfParent", "權益總額", "權益總計", "股東權益總額", "Equity"], ["非控制", "其他", "報酬"]),
    "assets":    (["TotalAssets", "資產總額", "資產總計"], []),
    "cur_assets":(["CurrentAssets", "流動資產"], ["非流動"]),
    "cur_liab":  (["CurrentLiabilities", "流動負債"], ["非流動"]),
    # 現金流量表（同為損益式累計數，需還原單季）
    "ocf":       (["CashProvidedByOperatingActivities", "CashFlowsFromOperatingActivities", "NetCashProvidedByOperating", "營業活動之淨現金"], ["投資", "籌資", "Investing", "Financing"]),
    "capex":     (["AcquisitionOfPropertyPlantAndEquipment", "PaymentsToAcquirePropertyPlantAndEquipment", "取得不動產、廠房及設備", "購置不動產"], ["處分", "出售", "Proceeds", "Disposal"]),
}


def enabled() -> bool:
    return bool(os.environ.get("FINMIND_TOKEN", "").strip())


def _token() -> str:
    return os.environ.get("FINMIND_TOKEN", "").strip()


def _is_quota_err(status_code, j) -> bool:
    """是否為額度／頻率上限錯誤：HTTP 402/429，或訊息含 upper limit / reach / limit。"""
    if status_code in (402, 429):
        return True
    msg = str((j or {}).get("msg", "")).lower()
    return ("upper limit" in msg) or ("reach" in msg and "limit" in msg) or ("too many" in msg)


def _fetch(dataset: str, data_id: str, start: str) -> List[dict]:
    if circuit_open():          # 熔斷中：直接略過，不連線（避免卡逾時、也避免把封鎖拉更長）
        return []
    tok = _token()
    for mode in (["header", "param", "anon"] if tok else ["anon"]):      # token 放標頭被拒→放參數→最後才免登入
        try:
            headers = {"Authorization": f"Bearer {tok}"} if mode == "header" else {}
            prm = {"dataset": dataset, "data_id": data_id, "start_date": start}
            if mode == "param":
                prm["token"] = tok
            r = requests.get(FINMIND_URL, params=prm,
                             headers=headers, timeout=(5, 12))      # 讀取逾時由 25→12 秒，額度滿時不拖垮個股頁
            try:
                j = r.json()
            except Exception:
                j = {}
            if r.ok and j.get("status", 200) == 200:
                return j.get("data") or []
            if _is_quota_err(r.status_code, j):      # 額度上限 → 開熔斷、停止後續嘗試
                _trip()
                return []
            logger.warning(f"finmind {dataset} {data_id}: HTTP {r.status_code} status {j.get('status')} {str(j.get('msg', ''))[:60]}")
        except Exception as e:
            logger.warning(f"finmind {dataset} {data_id}: {type(e).__name__}")
    return []


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _period(date_str: str) -> Optional[str]:
    """'2026-03-31' → '2026Q1'（依月份分季）。"""
    s = str(date_str or "")
    if len(s) < 7 or s[4] != "-":
        return None
    try:
        y = s[:4]; mth = int(s[5:7])
    except Exception:
        return None
    return f"{y}Q{(mth - 1) // 3 + 1}"


def _match(type_name: str, key: str) -> bool:
    inc, exc = _ALIAS[key]
    t = str(type_name)
    if any(x.lower() in t.lower() for x in exc):
        return False
    return any(s.lower() in t.lower() for s in inc)


def _pivot(rows: List[dict]) -> Dict[str, Dict[str, float]]:
    """把長格式 [{date,type,value}] 轉成 {date: {標準科目: 值}}（每個標準科目取第一個命中的 type）。"""
    byd: Dict[str, Dict[str, float]] = {}
    for r in rows:
        d = r.get("date"); t = r.get("type"); v = _num(r.get("value"))
        if not d or t is None or v is None:
            continue
        slot = byd.setdefault(d, {})
        for key in _ALIAS:
            if key not in slot and _match(t, key):
                slot[key] = v
                break
    return byd


def _decumulate(byd: Dict[str, Dict[str, float]], field: str) -> Dict[str, float]:
    """損益類還原單季：同一會計年度內，本季單季 = 本季累計 − 上一季累計；每年第一季取原值。
    回傳 {date: 單季值}。只處理有該 field 的日期。"""
    dates = sorted(d for d in byd if field in byd[d])
    out: Dict[str, float] = {}
    prev_by_year: Dict[str, float] = {}
    for d in dates:
        year = d[:4]
        cum = byd[d][field]
        q = _period(d)
        if q and q.endswith("Q1"):
            out[d] = cum
        elif year in prev_by_year:
            out[d] = cum - prev_by_year[year]
        else:
            out[d] = cum                     # 年度中缺前季，退而用累計（避免整季消失）
        prev_by_year[year] = cum
    return out


def _single(byd: Dict[str, Dict[str, float]], field: str) -> Dict[str, float]:
    """綜合損益表：FinMind 的 TaiwanStockFinancialStatements 本來就是「單季數」（2026-10-12 以台積電、鴻海實測：
    台積電 2025 Q1~Q4 EPS 13.95／15.36／17.44／19.51，與官方單季一致），不可再相減。只有現金流量表才是年度累計。"""
    return {d: byd[d][field] for d in sorted(byd) if field in byd[d]}


def financial_indicators(code: str) -> Optional[dict]:
    """回傳各指標的季度序列：{metric: [{period, value}], ...}；未啟用或無資料回 None。"""
    if not enabled():
        return None
    code = str(code).strip()
    with _lock:
        ent = _cache.get(code)
        if _cache_fresh(ent):
            return ent["data"]
    dbkey = f"finmind_fin_{code}"
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 5 * 365 * 86400))
    inc = _pivot(_fetch("TaiwanStockFinancialStatements", code, start))
    bs = _pivot(_fetch("TaiwanStockBalanceSheet", code, start))
    cf = _pivot(_fetch("TaiwanStockCashFlowsStatement", code, start))
    data = _build(inc, bs, cf)
    if data:
        with _lock:
            _cache[code] = {"at": time.time(), "data": data}
        _db_put(dbkey, data)            # 持久化最近一次正確結果，供額度滿/連線失敗時回退
        return data
    # live 抓不到（熔斷中或上游失敗）→ 回退資料庫最近一次正確季報（季報非盤中變動，安全且符合「不要錯誤資訊」）
    cached = _db_get(dbkey)
    if cached and cached.get("data"):
        with _lock:
            _cache[code] = {"at": time.time(), "data": cached["data"], "fallback": True}
        return cached["data"]
    return data


def _build(inc: Dict[str, Dict[str, float]], bs: Dict[str, Dict[str, float]],
           cf: Optional[Dict[str, Dict[str, float]]] = None) -> Optional[dict]:
    # 單季損益
    rev_q = _single(inc, "revenue")
    gross_q = _single(inc, "gross")
    op_q = _single(inc, "op_income")
    net_q = _single(inc, "net_income")
    eps_q = _single(inc, "eps")
    out: Dict[str, List[dict]] = {}

    def series(dates_vals):
        rows = []
        for d in sorted(dates_vals):
            p = _period(d)
            if p and dates_vals[d] is not None:
                rows.append({"period": p, "value": round(dates_vals[d], 2)})
        return rows

    def ratio(numer_q, denom_q):
        r = {}
        for d in numer_q:
            dn = denom_q.get(d)
            if dn:
                r[d] = numer_q[d] / dn * 100
        return r

    if eps_q:
        out["eps"] = series(eps_q)
    if gross_q and rev_q:
        out["gross"] = series(ratio(gross_q, rev_q))
    if op_q and rev_q:
        out["op"] = series(ratio(op_q, rev_q))
    if net_q and rev_q:
        out["net"] = series(ratio(net_q, rev_q))
    # 資產負債表為時點數（不還原）；ROE/ROA 用單季淨利 ÷ 期末權益／資產
    equity = {d: bs[d]["equity"] for d in bs if "equity" in bs[d]}
    assets = {d: bs[d]["assets"] for d in bs if "assets" in bs[d]}
    ca = {d: bs[d]["cur_assets"] for d in bs if "cur_assets" in bs[d]}
    cl = {d: bs[d]["cur_liab"] for d in bs if "cur_liab" in bs[d]}
    if net_q and equity:
        out["roe"] = series(ratio(net_q, equity))
    if net_q and assets:
        out["roa"] = series(ratio(net_q, assets))
    if ca and cl:
        out["current_ratio"] = series(ratio(ca, cl))
    if equity and assets:
        out["equity_ratio"] = series(ratio(equity, assets))
    # 自由現金流（單季）＝營業活動現金流 − 資本支出（取絕對值，避免 capex 正負號在不同來源不一致）
    if cf:
        ocf_q = _decumulate(cf, "ocf")
        capex_q = _decumulate(cf, "capex")
        if ocf_q:
            fcf = {}
            for d, ocf in ocf_q.items():
                cx = capex_q.get(d)
                fcf[d] = ocf - abs(cx) if cx is not None else ocf
            out["fcf"] = series(fcf)
    return out or None


def schema_probe(code: str = "2382") -> dict:
    """診斷用（站主）：回傳 FinMind 財報三張表的實際欄位（type／origin_name）與最新一期原始列，
    讓我們用真實回應核對欄位名稱、以已知個股驗證數字後，再精準重新開啟財務比率。"""
    if not enabled():
        return {"enabled": False, "note": "未設定 FINMIND_TOKEN"}
    code = str(code).strip()
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
    out = {"enabled": True, "code": code, "datasets": {}}
    for ds in ("TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet", "TaiwanStockCashFlowsStatement"):
        rows = _fetch(ds, code, start)
        latest = max((r.get("date") for r in rows if r.get("date")), default=None)
        types = {}
        for r in rows:
            t = str(r.get("type"))
            if t not in types:
                types[t] = str(r.get("origin_name") or "")
        latest_rows = [{"type": r.get("type"), "value": r.get("value"), "origin_name": r.get("origin_name")}
                       for r in rows if r.get("date") == latest]
        out["datasets"][ds] = {"n_rows": len(rows), "latest_date": latest,
                               "types": types, "latest_rows": latest_rows[:120]}
    return out


# ── 三大法人（個股逐日買賣超）：補上櫃(.TWO)個股，並在證交所 T86 失效時作為上市的備援顯示來源 ──
# FinMind 資料集 TaiwanStockInstitutionalInvestorsBuySell：date / stock_id / buy / sell / name。
# name 為機構別，相容英文代碼與中文名稱；買賣超(net)＝buy−sell（股），再換算成「張」。
# 安全：只認得出的機構別才計入；若某日認不出「外資／投信」任一腿，該日直接略過（不臆測、不輸出錯誤數字）。
_INST_NAME = {
    "foreign":       ("Foreign_Investor", "Foreign_Investors", "外陸資", "外資及陸資", "外資"),
    "foreign_dealer":("Foreign_Dealer_Self", "外資自營商"),
    "trust":         ("Investment_Trust", "投信"),
    "dealer_self":   ("Dealer_self", "Dealer_Self", "自營商(自行買賣)", "自營商自行買賣"),
    "dealer_hedge":  ("Dealer_Hedging", "自營商(避險)", "自營商避險"),
    "dealer_all":    ("Dealer",),            # 有些版本只給合併的自營商
}


def _inst_leg(name: str) -> Optional[str]:
    n = str(name or "").strip()
    for leg, names in _INST_NAME.items():
        if n in names:
            return leg
    return None


def _lots(shares) -> Optional[int]:
    try:
        s = float(shares)
    except Exception:
        return None
    return int((abs(s) + 500) // 1000) * (1 if s >= 0 else -1)


def institutional(code: str, days: int = 30) -> Optional[list]:
    """個股近 days 日三大法人買賣超（張），新到舊：[{date,foreign,trust,dealer,total}]。
    未啟用（無 token）或資料不足回 None。嚴格對應機構名稱，認不出就略過該日。"""
    if not enabled():
        return None
    code = str(code).strip()
    ck = f"inst:{code}"
    dbkey = f"finmind_inst_{code}"
    with _lock:
        ent = _cache.get(ck)
        if ent and (ent.get("fallback") and time.time() - ent["at"] < _FALLBACK_TTL
                    or not ent.get("fallback") and time.time() - ent["at"] < 3 * 3600):
            return ent["data"]
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days + 10) * 86400))
    rows = _fetch("TaiwanStockInstitutionalInvestorsBuySell", code, start)
    byd: Dict[str, Dict[str, float]] = {}
    for r in rows:
        d = r.get("date"); leg = _inst_leg(r.get("name"))
        if not d or leg is None:
            continue
        net = None
        if r.get("buy") is not None or r.get("sell") is not None:
            try:
                net = float(r.get("buy") or 0) - float(r.get("sell") or 0)
            except Exception:
                net = None
        if net is None:
            continue
        byd.setdefault(d, {})[leg] = byd.setdefault(d, {}).get(leg, 0.0) + net
    out = []
    for d in sorted(byd, reverse=True):
        legs = byd[d]
        if "foreign" not in legs or "trust" not in legs:   # 認不出主要腿 → 略過該日（不臆測）
            continue
        foreign = legs.get("foreign", 0.0)
        trust = legs.get("trust", 0.0)
        dealer = legs.get("dealer_all")
        if dealer is None:
            dealer = legs.get("dealer_self", 0.0) + legs.get("dealer_hedge", 0.0)
        total = foreign + legs.get("foreign_dealer", 0.0) + trust + dealer
        row = {"date": str(d)[:10], "foreign": _lots(foreign), "trust": _lots(trust),
               "dealer": _lots(dealer), "total": _lots(total)}
        if any(v is None for v in row.values()):
            continue
        out.append(row)
        if len(out) >= days:
            break
    if not out:
        # live 抓不到（熔斷中或上游失敗）→ 回退資料庫最近一次正確法人資料
        cached = _db_get(dbkey)
        if cached and cached.get("data"):
            with _lock:
                _cache[ck] = {"at": time.time(), "data": cached["data"], "fallback": True}
            return cached["data"]
        return None
    with _lock:
        _cache[ck] = {"at": time.time(), "data": out}
    _db_put(dbkey, out)
    return out


def institutional_probe(code: str = "2330") -> dict:
    """診斷用（站主）：回傳 FinMind 個股法人資料的實際 name 值與最新一期原始列，
    讓我們用真實回應核對機構名稱後再放心採用。"""
    if not enabled():
        return {"enabled": False, "note": "未設定 FINMIND_TOKEN"}
    code = str(code).strip()
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 20 * 86400))
    rows = _fetch("TaiwanStockInstitutionalInvestorsBuySell", code, start)
    names = sorted({str(r.get("name")) for r in rows if r.get("name") is not None})
    latest = max((r.get("date") for r in rows if r.get("date")), default=None)
    latest_rows = [r for r in rows if r.get("date") == latest]
    return {"enabled": True, "code": code, "n_rows": len(rows), "latest_date": latest,
            "distinct_names": names, "recognized": {n: _inst_leg(n) for n in names},
            "latest_rows": latest_rows[:20]}


def news(code: str, days: int = 14, limit: int = 10) -> Optional[list]:
    """個股近期新聞（FinMind TaiwanStockNews：date/title/source/link/description）。
    未啟用回 None；取最近 days 天、最新 limit 則。"""
    if not enabled():
        return None
    code = str(code).strip()
    ck = f"news:{code}"
    with _lock:
        ent = _cache.get(ck)
        if ent and time.time() - ent["at"] < 3600:
            return ent["data"]
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    rows = _fetch("TaiwanStockNews", code, start)
    out = []
    for r in rows:
        t = str(r.get("title") or "").strip()
        if not t:
            continue
        out.append({"date": str(r.get("date") or "")[:16], "title": t[:120],
                    "source": str(r.get("source") or "").strip()[:30],
                    "link": str(r.get("link") or "").strip()})
    out.sort(key=lambda x: x["date"], reverse=True)
    out = out[:limit]
    with _lock:
        _cache[ck] = {"at": time.time(), "data": out}
    return out
