"""市場狀態（Market Regime）可解釋 baseline（2026-11-08，規格 §6 / P1-2 第一步）。

規格要求先建「可解釋規則 baseline」再談 HMM/GMM/LightGBM。本模組用已抓到的大盤資料組出
多維度、可解釋的市場狀態：趨勢（Trend）、波動（Volatility）、流動性（Liquidity），各自是
帶門檻的規則標籤＋原始指標＋來源。資料缺一律標 unknown/暫無（correct-or-暫無），不臆造。

重要界線：本模組**只做研究與顯示**，不接進線上訊號產生或放空閘門（那維持現狀的
fetch_market_regime()）。策略路由（依 regime 切換可啟用策略）需 OOS 驗證與更多資料，
且目前樣本期間過短，尚不足以證明 regime 的增量價值——故此處只建地基、不改決策。
廣度（Breadth，個股站上均線比例／漲跌家數）需逐檔均線資料，成本較高，暫列未建、標暫無。
"""
import logging
import math

logger = logging.getLogger(__name__)

_TREND_ZH = {"bullish": "多頭", "bearish": "空頭", "neutral": "橫盤", "unknown": "暫無"}


def _realized_vol_annual(closes, n=20):
    """近 n 日日報酬的年化已實現波動率（%）。資料不足回 None。"""
    try:
        cs = [float(c) for c in closes if c]
        if len(cs) < n + 1:
            if len(cs) < 6:
                return None
            n = len(cs) - 1
        rets = [math.log(cs[i] / cs[i - 1]) for i in range(len(cs) - n, len(cs)) if cs[i - 1] > 0]
        if len(rets) < 3:
            return None
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
        return round(sd * math.sqrt(252) * 100, 1)
    except Exception as e:
        logger.warning(f"regime._realized_vol_annual: {e}")
        return None


def _vol_state(vol):
    """台股加權指數年化波動的規則分級（heuristic baseline，可日後校準）。"""
    if vol is None:
        return "unknown", "暫無"
    if vol < 12:
        return "low", "低波動"
    if vol <= 22:
        return "normal", "正常波動"
    return "high", "高波動"


def _liquidity_state(stats):
    """以近 5 日平均成交金額 vs 前 20 日平均判斷放量/縮量。stats：{iso_date:{value,...}}。"""
    try:
        if not stats:
            return "unknown", "暫無", None
        days = sorted(stats)
        vals = [float(stats[d].get("value") or 0) for d in days if stats[d].get("value")]
        if len(vals) < 10:
            return "unknown", "暫無", None
        recent = vals[-5:]
        prior = vals[-25:-5] if len(vals) >= 25 else vals[:-5]
        if not prior:
            return "unknown", "暫無", None
        r = (sum(recent) / len(recent)) / (sum(prior) / len(prior))
        ratio = round(r, 2)
        if r >= 1.15:
            return "high", "放量", ratio
        if r <= 0.85:
            return "low", "縮量", ratio
        return "normal", "量能持平", ratio
    except Exception as e:
        logger.warning(f"regime._liquidity_state: {e}")
        return "unknown", "暫無", None


def regime_snapshot() -> dict:
    """回傳多維度市場狀態 baseline（研究/顯示用，不接線上決策）。"""
    out = {"ok": True, "components": {}, "note": "規則式 baseline；僅供研究與顯示，未接入線上訊號決策。"}
    # 趨勢（沿用既有中期趨勢判斷）
    trend_state, trend_zh = "unknown", "暫無"
    try:
        from data_fetcher import fetch_market_regime
        tr = fetch_market_regime() or {}
        if tr.get("available"):
            trend_state = tr.get("trend", "unknown")
            trend_zh = _TREND_ZH.get(trend_state, "暫無")
        out["components"]["trend"] = {
            "state": trend_state, "label": trend_zh,
            "ma20": tr.get("ma20"), "ma60": tr.get("ma60"), "ma60_slope_pct": tr.get("ma60_slope_pct"),
            "source": "TWSE 官方加權指數日收盤"}
    except Exception as e:
        logger.warning(f"regime trend: {e}")
        out["components"]["trend"] = {"state": "unknown", "label": "暫無"}
    # 波動
    vol = None
    try:
        from data_fetcher import _twse_index_daily_closes
        vol = _realized_vol_annual(_twse_index_daily_closes(months=3))
    except Exception as e:
        logger.warning(f"regime vol: {e}")
    vstate, vzh = _vol_state(vol)
    out["components"]["volatility"] = {"state": vstate, "label": vzh, "annualized_pct": vol,
                                       "source": "加權指數近 20 日報酬年化"}
    # 流動性
    lstate, lzh, lratio = "unknown", "暫無", None
    try:
        from data_fetcher import twse_market_stats
        lstate, lzh, lratio = _liquidity_state(twse_market_stats(months=2))
    except Exception as e:
        logger.warning(f"regime liquidity: {e}")
    out["components"]["liquidity"] = {"state": lstate, "label": lzh, "recent_vs_prior": lratio,
                                      "source": "TWSE FMTQIK 上市成交金額"}
    # 廣度：未建（需逐檔均線資料），誠實標暫無
    out["components"]["breadth"] = {"state": "unknown", "label": "暫無",
                                    "note": "需逐檔站上均線比例／漲跌家數，成本較高，尚未建置"}
    # 綜合白話
    out["composite"] = _compose(trend_zh, vzh, lzh)
    return out


def _compose(trend_zh, vol_zh, liq_zh):
    parts = []
    if trend_zh != "暫無":
        parts.append(f"趨勢{trend_zh}")
    if vol_zh != "暫無":
        parts.append(vol_zh)
    if liq_zh != "暫無":
        parts.append(liq_zh)
    if not parts:
        return "市場狀態資料暫無"
    return "、".join(parts)
