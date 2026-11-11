"""美編 P1-C（VISUAL_DESIGN_PLAN P1-C／brief §4 總覽）：總覽重排＋今日狀態卡＋即時/日線分區時間。

- 上＝需處理風險＋資料可信狀態（#v4o-attn）＋今日訊號/持倉/通知；中＝圖與機會；下＝市場背景。
- 需處理定義重用持倉頁同一套（breached／cushion<3／stop_suggest），不另創門檻。
- 加權指數卡：即時主數值標「報價 HH:MM」、日線標「{日期} 收盤」；開高低昨收標明日線時點。
"""
import os
import re


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_attention_card_first_then_risk_then_charts():
    s = _dash()
    v4o = s[s.index('<div class="v4o" id="v4o">'):s.index('id="v4o-legacy"')]
    order = [v4o.index('aria-label="今日狀態"'), v4o.index('aria-label="今日訊號"'),
             v4o.index('aria-label="持倉追蹤"'), v4o.index('aria-label="通知"'),
             v4o.index('aria-label="加權指數走勢"'), v4o.index('aria-label="自選股"'),
             v4o.index('aria-label="市場脈動"'), v4o.index('aria-label="全球市場"'),
             v4o.index('aria-label="本週財經日曆"')]
    assert order == sorted(order)                       # 上=狀態/風險、中=圖與機會、下=市場背景
    assert v4o.count('id="v4o-sig"') == 1 and v4o.count('id="v4o-pos"') == 1   # 卡片只搬家、不重複


def test_attention_reuses_positions_page_definitions():
    s = _dash()
    i = s.index("function v4oAttn")
    blk = s[i:i + 3000]
    assert "p.breached" in blk and "待盤後結算確認" in blk
    assert "p.cushion_pct!=null&&p.cushion_pct<3" in blk          # 與持倉頁同一門檻
    assert "stop_suggest" in blk
    assert "未取得" in blk and "載入中" in blk                      # 缺資料誠實呈現
    assert "已是最新" in blk and "尚未更新" in blk                  # 資料可信狀態


def test_index_card_separates_live_vs_daily_asof():
    s = _dash()
    i = s.index("const ixLive=ix.price!=null")
    blk = s[i:i + 600]
    assert "報價 '+(toTaipeiHM(mo.fetched_at)" in blk              # 即時→報價 HH:MM
    assert "收盤'" in blk                                          # 日線→{日期} 收盤
    assert "開高低昨收為 ${dt} 日線" in s                           # 日線時點標註
    assert "非同一時點，不可互相驗證價位關係" in s                   # LIVE08 同口徑
