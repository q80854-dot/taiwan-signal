"""美編 P2-C（brief §8.4 B04）：研究脈絡一致。

- 策略版本/參數凍結日取自單一事實來源 risk_basis（config_version＝config_hash[:12]），
  訊號頁/持倉頁/策略學習頁一致顯示，不前端猜值。
- run_id 後端未記錄 → 誠實列為資料契約缺口（不假造）。
- 歷史訊號依產生當時參數判讀（不以現行參數回填）。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_single_source_of_truth():
    s = _dash()
    i = s.index("async function tsCtx")
    blk = s[i:i + 600]
    assert "/api/public/summary" in blk and "config_version" in blk
    assert "cfg:null" in blk                             # 取不到→null→顯示未取得，不猜值
    assert "未取得" in s[s.index("function tsCtxLine"):s.index("function tsCtxLine") + 500]


def test_run_id_gap_honest():
    s = _dash()
    assert s.count("run_id：後端未記錄") >= 2             # 訊號/學習共用行＋持倉風險卡


def test_three_pages_mounted():
    s = _dash()
    assert 'id="sig-ctx"' in s and "tsCtxLine(` · 批次" in s       # 訊號頁含批次 as_of
    assert 'id="lrn-ctx"' in s                                      # 策略學習頁
    assert s.count("參數版本 ${rk.config_version?'champion-'") == 2  # 持倉：風險設定卡＋風險監控卡
    assert "歷史訊號依產生當時參數判讀" in s
