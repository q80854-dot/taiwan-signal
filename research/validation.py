"""研究驗證工具：Deflated Sharpe Ratio（DSR）與回測過度配適機率（PBO／CSCV）。
（研究工具；不被 app 匯入、不影響任何線上行為。2026-11-13 外部審查「統計驗證」項目。）

為什麼需要：同一批資料上試過愈多方案，「最好的那個」看起來愈漂亮，即使全部都沒有真本事。
  · DSR（Bailey & López de Prado, 2014）：把「試了 N 個方案」「報酬不是常態」「樣本長度」都扣掉之後，
    被選中方案的 Sharpe 仍大於 0 的機率。接近 1 才算站得住；0.5 上下等於分不出來。
  · PBO（Bailey, Borwein, López de Prado, Zhu, 2015；CSCV）：把時間切成 S 段，取一半當樣本內、一半當樣本外，
    所有切法都試一遍——樣本內最好的方案，到樣本外落在後半段的比例。愈接近 0 愈好；0.5 代表「挑最好的」毫無意義。

輸入都是「每個訊號日一個數字」的序列（同一天的訊號先取平均），避免把同一天連動的交易當成獨立樣本。
"""
import itertools
import math
from statistics import NormalDist

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def _moments(xs):
    n = len(xs)
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(v)
    if sd == 0:
        return m, 0.0, 0.0, 3.0
    skew = sum(((x - m) / sd) ** 3 for x in xs) / n
    kurt = sum(((x - m) / sd) ** 4 for x in xs) / n
    return m, sd, skew, kurt


def sharpe(xs):
    """每期（未年化）Sharpe。標準差為 0 回 0。"""
    m, sd, _, _ = _moments(xs)
    return m / sd if sd else 0.0


def expected_max_sharpe(n_trials, sr_variance):
    """N 個毫無真本事的方案中，最大 Sharpe 的期望值（DSR 的門檻 SR0）。"""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    a = _N.inv_cdf(1 - 1 / n_trials)
    b = _N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(xs, n_trials, sr_variance):
    """回傳 {sr, sr0, dsr, n, skew, kurt}。dsr＝扣掉多重嘗試、偏態、峰態後，真實 Sharpe > 0 的機率。
    sr_variance＝所有試過方案的（每期）Sharpe 的變異數；n_trials＝試過的方案總數（含失敗的）。"""
    n = len(xs)
    m, sd, skew, kurt = _moments(xs)
    sr = m / sd if sd else 0.0
    sr0 = expected_max_sharpe(n_trials, sr_variance)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if n < 3 or denom <= 0:
        return {"sr": sr, "sr0": sr0, "dsr": None, "n": n, "skew": skew, "kurt": kurt}
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return {"sr": sr, "sr0": sr0, "dsr": _N.cdf(z), "n": n, "skew": skew, "kurt": kurt}


def pbo_cscv(matrix, n_blocks=10):
    """matrix：{方案名: [每期報酬…]}，各方案等長、同一批日期。回傳 {pbo, n_splits, logits, best_is_counts}。
    每種切法：樣本內 Sharpe 最高的方案 → 它在樣本外的相對名次 ω（0～1，愈大愈好）→ logit(ω)。
    PBO＝logit ≤ 0（樣本外落在後半）的比例。"""
    names = list(matrix)
    T = len(matrix[names[0]])
    if len(names) < 2 or T < n_blocks * 2 or n_blocks % 2:
        return {"pbo": None, "n_splits": 0, "logits": [], "best_is_counts": {}}
    size = T // n_blocks
    blocks = [range(i * size, (i + 1) * size if i < n_blocks - 1 else T) for i in range(n_blocks)]
    logits, counts = [], {}
    for is_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        is_set = set(is_idx)
        is_rows = [t for b in is_idx for t in blocks[b]]
        os_rows = [t for b in range(n_blocks) if b not in is_set for t in blocks[b]]
        sr_is = {k: sharpe([matrix[k][t] for t in is_rows]) for k in names}
        sr_os = {k: sharpe([matrix[k][t] for t in os_rows]) for k in names}
        best = max(names, key=lambda k: sr_is[k])
        counts[best] = counts.get(best, 0) + 1
        rank = sum(1 for k in names if sr_os[k] <= sr_os[best])          # 1＝樣本外最差，N＝最好
        w = rank / (len(names) + 1)
        logits.append(math.log(w / (1 - w)))
    return {"pbo": sum(1 for x in logits if x <= 0) / len(logits), "n_splits": len(logits),
            "logits": logits, "best_is_counts": counts}


def daily_series(rows):
    """[(日期, 值)] → 依日期排序的每日平均序列與日期清單。"""
    by = {}
    for d, v in rows:
        by.setdefault(d, []).append(v)
    days = sorted(by)
    return days, [sum(by[d]) / len(by[d]) for d in days]
