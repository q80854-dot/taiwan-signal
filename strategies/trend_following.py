"""策略 1：Trend Following 趨勢追蹤（§三 3.1，階段 2a）。

只做多、順勢。重點是「多頭排列＋強趨勢＋週線不偏空」，並避免價格過度延伸後追高。
出場價位/部位/成本一律走既有共用函式（calc_stop_loss_tw / calc_take_profits_tw），與 Champion
同口徑，日後可直接比較。**純 shadow：本模組不發訊號、不接掃描決策、不影響線上訊號。**
"""
from typing import Optional

from .base import Strategy


class TrendFollowing(Strategy):
    name = "trend_following"
    direction = "long"
    label = "趨勢追蹤"
    hypothesis = {
        "id": "H-TF-01",
        "metric": "以 Champion 同口徑分批出場計算的平均淨 R（avg_champion_r）",
        "acceptance": "已結算樣本 ≥ 30 筆且平均淨 R 顯著 > 0（叢集拔靴 95% 下界 > 0）才視為有證據；達不到即保留為資料不足／未達標",
        "rationale": "多頭排列＋強 ADX＋週線不偏空＋未過度延伸，順勢做多應有正期望；避免追高以降低回撤。",
    }

    adx_min = 25            # 強趨勢門檻（ADX）
    rsi_overheated = 72     # RSI 高於此視為過度延伸，避免追高

    def regime_applicability(self, regime: dict) -> float:
        """趨勢追蹤在趨勢型 regime 較有效、盤整期較差。階段 2a 先恆啟用（1.0）；
        regime 加權待 PIT regime 分類器（階段 4）接上後再細化。"""
        return 1.0

    def generate(self, ticker, stock_info, tf_data, market_overview, inst_data=None) -> Optional[dict]:
        from signal_engine import check_multi_timeframe_tw, calc_stop_loss_tw, calc_take_profits_tw
        from indicators import calc_all_indicators

        daily = (tf_data or {}).get("daily")
        if not daily:
            return None
        ind = calc_all_indicators(daily)
        if not ind.get("valid"):
            return None
        mtf = check_multi_timeframe_tw(tf_data)

        # ── 趨勢追蹤做多門檻 ──
        if mtf.get("direction") != "buy":                       # 需多頭方向
            return None
        if (mtf.get("adx_value") or 0) < self.adx_min:          # 需強趨勢
            return None
        if "bearish" in (mtf.get("weekly_bias") or ""):         # 週線不得偏空（逆勢）
            return None
        ema = ind.get("ema") or {}
        if "bullish" not in (ema.get("bias") or ""):            # 均線多頭排列
            return None
        if (mtf.get("rsi_value") or 50) > self.rsi_overheated:  # 避免過度延伸後追高
            return None

        price = ind.get("current_price")
        atr = (ind.get("atr") or {}).get("value")
        if not price or not atr:
            return None

        size_cat = (stock_info or {}).get("size_cat", "中型股")
        lows = daily.get("lows") or []
        low_5d = min(lows[-5:]) if len(lows) >= 5 else None
        stop = calc_stop_loss_tw("buy", price, atr, ind, size_cat, low_5d, None)
        tps = calc_take_profits_tw("buy", price, stop, size_cat)
        return {"direction": "long", "entry": round(price, 2), "stop": stop,
                "tp1": tps["tp1"], "tp2": tps["tp2"], "tp3": tps["tp3"],
                "score": mtf.get("score", 0),
                "reason": "趨勢追蹤：多頭排列＋強ADX＋週線不偏空＋未過度延伸"}
