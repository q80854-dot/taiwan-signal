"""LIVE08（第三階段審查）：總覽日線 vs 即時時間混用（顯示層）。

主要指數 tile 的主數值可能是即時報價或 fallback 日線收盤（不同 session）：各 tile 須帶各自
as_of（「報價 HH:MM」vs「{日期} 收盤」），不以全頁更新時間代替欄位實際時間；
即時主數值不得與日線開高低互相驗證價位關係（分時卡開高低收已另標日線日期）。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_tile_asof_param_exists():
    s = _dash()
    assert "function x6Mini(n,tag,v,ch,sp,dg=2,asof)" in s


def test_twii_tile_distinguishes_live_vs_daily_close():
    s = _dash()
    assert "twiiLive" in s
    assert "報價 '+(_moHM||'最新')" in s        # 即時 → 報價 HH:MM
    assert "收盤':null)" in s                    # fallback → {日期} 收盤
    # 不得混標的說明註記存在
    assert "不得混標" in s or "session 不同" in s


def test_intraday_card_ohlc_labeled_daily():
    s = _dash()
    assert "開高低收為" in s and "證交所日線" in s
