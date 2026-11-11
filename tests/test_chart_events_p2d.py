"""美編 P2-D（brief §8.4 B05/B06）：圖表比較標示＋事件定位。

- B05：持倉頁權益曲線標「基準同期間（起點=100）、費後（估列滑價/手續費/證交稅、放空含
  借券費——與 calc_tw_pnl 實際結算口徑一致）、最大回撤取自績效彙總（不前端重算）」；
  學習頁累積淨 R 標費後口徑。
- B06：個股頁日 K 標本檔訊號事件——▲▼ 訊號日／◆ 結案日，全為真實 signal_history 紀錄、
  無資料即不畫；<title> tooltip 含來源與價格定義（訊號日收盤／依結算規則）；
  事件層置於 hit 層之上才能 hover。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_b05_equity_curve_labels():
    s = _dash()
    i = s.index("const eqC=v5Card")
    blk = s[i - 400:i + 600]
    assert "基準同期間" in blk and "起點=100" in blk.replace("起始日為 100", "起點=100") or "以起始日為 100" in blk
    assert "費後（估列滑價/手續費/證交稅" in blk and "借券費" in blk
    assert "pf.max_drawdown" in blk and "未取得" in blk          # 回撤取績效彙總、缺就說未取得
    assert "不前端重算指標" in s[i - 400:i]


def test_b05_learning_net_r_fee_basis():
    s = _dash()
    assert "淨 R＝費後，估列滑價/手續費/證交稅、放空含借券費" in s


def test_b06_event_markers_real_data_only():
    s = _dash()
    i = s.index("P2-D B06（事件定位）")
    blk = s[i:i + 2200]
    assert "signal_history" in blk                                # 真實紀錄
    assert "h.result&&h.result!=='pending'" in blk                # 結案標記只畫已結案
    assert "訊號日收盤（來源：系統訊號紀錄）" in blk               # 價格定義＋來源
    assert "依結算規則" in blk and "訊號結算紀錄" in blk
    assert "<title>" in blk                                       # tooltip
    # 事件層在 hit 層之後（才能 hover 出 title）
    assert 'id="v5-hit"/>${evSvg}' in s
    assert "▲▼ 訊號日 · ◆ 結案日" in s                            # 圖例
