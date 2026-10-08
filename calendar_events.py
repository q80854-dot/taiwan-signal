"""行事曆與列管資料：近期除權息、處置／注意股。
來源皆為官方公開 API；欄位名稱用關鍵字比對，格式變動時回傳 error 與實際欄位，不安靜失敗。"""
import re, time, logging, datetime as dt
import requests

logger = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0"}
TWSE_EX = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
TPEX_EX = "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost"
_cache = {}


def _cached(key, ttl, fn):
    c = _cache.get(key)
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = fn()
    if v and not v.get("error"):
        _cache[key] = (time.time(), v)
    return v


def _tw_taipei_today():
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def _parse_date(raw):
    s = re.sub(r"\D", "", str(raw or ""))
    try:
        if len(s) == 7:
            return dt.date(int(s[:3]) + 1911, int(s[3:5]), int(s[5:7]))
        if len(s) == 8:
            return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return None


def _pick(keys, *needles, avoid=()):
    for k in keys:
        if any(n in k for n in needles) and not any(a in k for a in avoid):
            return k
    return None


def _fetch_ex(url, market):
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return [], f"{market} HTTP {r.status_code}"
    rows = r.json()
    if not rows:
        return [], None
    keys = list(rows[0].keys())
    # 優先用明確的「除權息日」欄位；只有找不到時才退回一般 Date（上櫃預告表可能同時有資料日期與除權息日期，
    # 若誤取資料日期，整張表會全部顯示成同一天）
    kd = (_pick(keys, "ExRightsExDividendDate", "ExDividendDate", "ExRightDate", "ExRightsDate", "除權息日", "除權除息日", "除息日", "除權日")
          or _pick(keys, "Date", "日期", avoid=("Announce", "公告", "Data", "資料")))
    kc = _pick(keys, "Code", "代號", "代碼")
    kn = _pick(keys, "Name", "名稱")
    kt = _pick(keys, "Exdividend", "權息", "Type", "除權息")
    kcash = _pick(keys, "CashDividend", "現金", "Cash")
    if not (kd and kc):
        return [], f"{market} 欄位無法辨識：{keys}"
    out = []
    for i in rows:
        d = _parse_date(i.get(kd))
        if not d:
            continue
        out.append({"date": d.isoformat(), "code": str(i.get(kc, "")).strip(),
                    "name": str(i.get(kn, "")).strip() if kn else "",
                    "type": str(i.get(kt, "")).strip() if kt else "",
                    "cash": str(i.get(kcash, "")).strip() if kcash else "",
                    "market": market})
    return out, None


def get_ex_dividend(days=21):
    def _do():
        today = _tw_taipei_today()
        items, errs = [], []
        for url, m in ((TWSE_EX, "上市"), (TPEX_EX, "上櫃")):
            try:
                rows, e = _fetch_ex(url, m)
                if e: errs.append(e)
                items += rows
            except Exception as ex:
                errs.append(f"{m}: {ex}")
        lo, hi = today.isoformat(), (today + dt.timedelta(days=days)).isoformat()
        items = sorted([x for x in items if lo <= x["date"] <= hi], key=lambda x: (x["date"], x["code"]))
        out = {"items": items, "errors": errs, "as_of": lo,
               "source": "TWSE OpenAPI TWT48U_ALL（上市）／TPEx OpenAPI tpex_exright_prepost（上櫃）"}
        if errs and not items:
            out["error"] = "；".join(errs)
        return out
    return _cached("ex", 3600, _do)


def get_listed_watch():
    """處置股、注意股（上市＋上櫃）。保留官方欄位，前端自行顯示。"""
    def _do():
        from stock_universe import TWSE_PUNISH_URL, TWSE_NOTICE_URL
        res = {"disposal": [], "attention": [], "errors": []}
        for url, key in ((TWSE_PUNISH_URL, "disposal"), (TWSE_NOTICE_URL, "attention")):
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                j = r.json()
                if j.get("stat") != "OK":
                    continue
                f = j.get("fields", [])
                for row in j.get("data", []):
                    d = dict(zip(f, row))
                    res[key].append({"code": str(d.get("證券代號", "")).strip(), "name": str(d.get("證券名稱", "")).strip(),
                                     "period": str(d.get("處置起迄時間") or d.get("公布日期") or d.get("日期") or "").strip(),
                                     "reason": str(d.get("處置條件") or d.get("注意交易資訊") or "").strip()[:80]})
            except Exception as e:
                res["errors"].append(f"{key}: {e}")
        try:   # 上櫃：TPEx OpenAPI（由 market_extras 取得，15 分鐘快取）
            import market_extras
            f = market_extras.get_flags()
            for key, src in (("disposal", "otc_disposal"), ("attention", "otc_attention")):
                for code, d in (f.get(src) or {}).items():
                    res[key].append({"code": code, "name": d.get("name", "") + "（櫃）", "period": d.get("period", ""), "reason": d.get("reason", "")})
            for e in f.get("errors", []):
                if "上櫃處置注意" in e:
                    res["errors"].append(e)
        except Exception as e:
            res["errors"].append(f"上櫃: {e}")
        res["source"] = "TWSE 處置股票／注意股票公告（上市）＋ TPEx OpenAPI 處置有價證券／注意股票（上櫃，名稱後標「櫃」）"
        return res
    return _cached("watch", 1800, _do)
