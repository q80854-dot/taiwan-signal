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
_cache: Dict[str, dict] = {}
_lock = threading.Lock()

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


def _fetch(dataset: str, data_id: str, start: str) -> List[dict]:
    tok = _token()
    for mode in (["header", "param", "anon"] if tok else ["anon"]):      # token 放標頭被拒→放參數→最後才免登入
        try:
            headers = {"Authorization": f"Bearer {tok}"} if mode == "header" else {}
            prm = {"dataset": dataset, "data_id": data_id, "start_date": start}
            if mode == "param":
                prm["token"] = tok
            r = requests.get(FINMIND_URL, params=prm,
                             headers=headers, timeout=(5, 25))
            try:
                j = r.json()
            except Exception:
                j = {}
            if r.ok and j.get("status", 200) == 200:
                return j.get("data") or []
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
        if ent and time.time() - ent["at"] < _TTL:
            return ent["data"]
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 5 * 365 * 86400))
    inc = _pivot(_fetch("TaiwanStockFinancialStatements", code, start))
    bs = _pivot(_fetch("TaiwanStockBalanceSheet", code, start))
    cf = _pivot(_fetch("TaiwanStockCashFlowsStatement", code, start))
    data = _build(inc, bs, cf)
    with _lock:
        _cache[code] = {"at": time.time(), "data": data}
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
