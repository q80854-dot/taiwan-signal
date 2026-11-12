"""臺灣 50 指數歷史成分股（point-in-time）。

用途：回測的標的池必須是「當時」的成分股，不能拿現在的名單回推歷史（倖存者偏差／前視偏誤）。
原本 stock_universe.get_tw50_components() 是一份寫死的 50 檔清單，既不是任何一天的真實成分，
也不隨時間變動（Q-P0-04）。

資料來源與可信度（務必如實看待）：
  · 使用者 2026-11-12 提供的研究彙整（臺灣指數公司季度審核新聞稿與技術通知），涵蓋
    2020-01-01～2026-11-12 共 88 筆列級異動（44 納入、44 剔除）。
  · 2019-12-31 基準為「重建式快照」（2019-11 公開持股名單＋2019-12 定審無進出），不是官方
    每日成分檔的直接快照。
  · 尚未以臺灣指數公司的付費每日成分檔核驗。本模組的 validate() 只能保證內部一致
    （不重複納入、不剔除非成分、檔數正確），不能保證與官方逐日名單完全相同。
  · 2019-12-31 以前沒有資料：members_on() 回 None，呼叫端不得假裝已知。

日期語意：公告日 ≠ 生效日。季度審核約早兩週公告；「正式成分」以 actual_effective_date 為準
（該日開盤起適用）。兩檔因處置而延後納入（南亞科 2025-12-31、欣興 2026-04-14），其間指數
暫為 49 檔——只存季度公告名單會把它們提早算成成分股。
"""
from datetime import date, datetime
from typing import Dict, List, Optional, Set

BASELINE_DATE = "2019-12-31"
VERIFIED_THROUGH = "2026-11-12"          # 資料整理截止日；此日之後的異動尚未收錄
SOURCE_NOTE = ("使用者提供之研究彙整（臺灣指數公司季度審核新聞稿與技術通知，2020-01-01～2026-11-12）；"
               "2019-12-31 基準為重建式快照；未經官方付費每日成分檔核驗")

BASELINE: List[str] = [
    "2330", "2317", "2454", "3008", "1301", "2412", "1303", "2891", "1216", "2882",
    "2881", "2886", "2884", "1326", "2308", "2002", "3711", "2885", "2892", "2474",
    "2880", "1101", "5880", "3045", "2207", "5876", "2912", "5871", "2382", "2303",
    "2357", "2801", "2887", "2883", "6505", "2890", "4938", "1402", "2327", "4904",
    "2395", "2888", "2301", "9904", "1102", "2105", "2633", "2823", "9910", "2408",
]

# (公告日, 原定生效日, [納入...], [剔除...])；納入項可寫成 (代號, 實際生效日) 表示延後生效。
_REVIEWS = [
    ("2020-06-05", "2020-06-22", ["6669"], ["9904"]),
    ("2020-09-04", "2020-09-21", ["2379", "6415"], ["2888", "2823"]),
    ("2020-12-04", "2020-12-21", ["3034"], ["2301"]),
    ("2021-03-05", "2021-03-22", ["1590", "8046"], ["2883", "2890"]),
    ("2021-06-04", "2021-06-21", ["2603", "2615", "2609", "2409"], ["2633", "6669", "2105", "2474"]),
    ("2021-09-03", "2021-09-22", ["8454"], ["1102"]),
    ("2021-12-03", "2021-12-20", ["3037"], ["1402"]),
    ("2022-03-04", "2022-03-21", ["2883"], ["4938"]),
    ("2022-06-02", "2022-06-20", ["6770"], ["8454"]),
    ("2022-09-02", "2022-09-19", ["2890"], ["6770"]),
    ("2022-12-02", "2022-12-19", ["1402"], ["2409"]),
    ("2023-03-03", "2023-03-20", ["1605"], ["8046"]),
    ("2023-06-03", "2023-06-19", ["4938"], ["6415"]),
    ("2023-09-01", "2023-09-18", ["2345", "2301", "3231", "6669"], ["1402", "1605", "2615", "2609"]),
    ("2024-03-01", "2024-03-18", ["3661"], ["9910"]),
    ("2024-06-07", "2024-06-24", ["3017"], ["2801"]),
    ("2024-09-06", "2024-09-23", ["6446"], ["2408"]),
    ("2024-12-06", "2024-12-23", ["2609"], ["1590"]),
    ("2025-03-07", "2025-03-24", ["2615"], ["1326"]),
    ("2025-06-06", "2025-06-23", ["2383"], ["3037"]),
    ("2025-09-05", "2025-09-22", ["6919", "2059"], ["6446", "1101"]),
    # 南亞科因處置有價證券延後，2025-12-31 才實際納入（技術通知）
    ("2025-12-05", "2025-12-22", ["3665", "2360", "3653", ("2408", "2025-12-31")], ["5871", "4938", "5876", "2609"]),
    # 欣興因處置延後，2026-04-14 才實際納入（技術通知）
    ("2026-03-06", "2026-03-23", ["2368", "7769", "2449", ("3037", "2026-04-14"), "2344"],
     ["3665", "3034", "2912", "2379", "2615"]),
    ("2026-06-05", "2026-06-22", ["3665", "3443", "8046", "4958"], ["6919", "2002", "1301", "2207"]),
    ("2026-09-04", "2026-09-21", ["6446"], ["3661"]),
]


def events() -> List[Dict]:
    """逐筆異動（每檔股票一列），依實際生效日排序；同日先剔除後納入不影響結果。"""
    out = []
    for ann, sched, adds, dels in _REVIEWS:
        for code in dels:
            out.append({"announcement_date": ann, "scheduled_effective_date": sched,
                        "actual_effective_date": sched, "action": "DELETE", "code": code, "delayed": False})
        for a in adds:
            code, actual = (a if isinstance(a, tuple) else (a, sched))
            out.append({"announcement_date": ann, "scheduled_effective_date": sched,
                        "actual_effective_date": actual, "action": "ADD", "code": code,
                        "delayed": actual != sched})
    out.sort(key=lambda e: (e["actual_effective_date"], 0 if e["action"] == "DELETE" else 1, e["code"]))
    return out


def _d(x) -> str:
    if isinstance(x, (date, datetime)):
        return x.strftime("%Y-%m-%d")
    return str(x)[:10]


def members_on(day) -> Optional[Set[str]]:
    """該日（開盤起）的正式成分股代號集合。早於基準日回 None（資料不存在，不猜）。"""
    ds = _d(day)
    if ds < BASELINE_DATE:
        return None
    cur = set(BASELINE)
    for e in events():
        if e["actual_effective_date"] > ds:
            break
        if e["action"] == "ADD":
            cur.add(e["code"])
        else:
            cur.discard(e["code"])
    return cur


def is_member(code, day) -> Optional[bool]:
    m = members_on(day)
    return None if m is None else str(code).split(".")[0] in m


def members_between(start, end) -> Set[str]:
    """[start, end] 期間曾是成分股的全部代號（回測要載入的標的池，含後來被剔除者）。"""
    s, e = max(_d(start), BASELINE_DATE), _d(end)
    out = set(members_on(s) or [])
    for ev in events():
        if s < ev["actual_effective_date"] <= e and ev["action"] == "ADD":
            out.add(ev["code"])
    return out


def announced_pending(day) -> List[Dict]:
    """已公告但尚未生效的異動（公告效應研究用；不得當成正式成分）。"""
    ds = _d(day)
    return [e for e in events() if e["announcement_date"] <= ds < e["actual_effective_date"]]


def validate() -> List[str]:
    """內部一致性檢查；回傳問題清單（空＝通過）。只證明自洽，不證明與官方逐日名單相同。"""
    problems = []
    if len(BASELINE) != 50 or len(set(BASELINE)) != 50:
        problems.append(f"基準應為 50 個唯一代號，實得 {len(set(BASELINE))}")
    cur = set(BASELINE)
    by_day: Dict[str, List[Dict]] = {}
    for e in events():
        by_day.setdefault(e["actual_effective_date"], []).append(e)
    for day in sorted(by_day):
        for e in by_day[day]:
            if e["action"] == "DELETE":
                if e["code"] not in cur:
                    problems.append(f"{day} 剔除 {e['code']}，但當時不是成分股")
                cur.discard(e["code"])
        for e in by_day[day]:
            if e["action"] == "ADD":
                if e["code"] in cur:
                    problems.append(f"{day} 納入 {e['code']}，但當時已是成分股")
                cur.add(e["code"])
        if len(cur) not in (49, 50):
            problems.append(f"{day} 之後成分數為 {len(cur)}（應為 50，延後納入期間為 49）")
    for ann, sched, adds, dels in _REVIEWS:
        if len(adds) != len(dels):
            problems.append(f"{ann} 公告的納入數（{len(adds)}）與剔除數（{len(dels)}）不相等")
        if ann >= sched:
            problems.append(f"{ann} 公告日不早於生效日 {sched}")
    return problems


def tickers(codes) -> List[str]:
    """代號 → yfinance 代碼（臺灣 50 成分皆為上市股）。"""
    return sorted(f"{c}.TW" for c in codes)
