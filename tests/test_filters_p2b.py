"""美編 P2-B（brief §8.4 B03/B09）：篩選脈絡＋更新不打擾。

- B03：訊號頁已選條件 chips（可逐一移除）＋重置＋結果數「顯示 X / 共 Y 檔」；
  純顯示、不新增保存檢視（故無本機/帳號範圍問題）。
- B09：自選股 60 秒自動更新——使用者聚焦清單內排序選單/按鈕/輸入（搜尋框除外）或
  選取文字時跳過本輪重畫；頁尾顯示最後成功更新時間；搜尋框焦點/游標還原（既有）。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_b03_chips_reset_and_count():
    s = _dash()
    assert 'id="sig-chips"' in s
    assert "sigClearFilter('${k}')" in s and "sigClearFilter('all')" in s        # 逐一移除＋全部重置
    assert "'dir'" in s[s.index("function sigClearFilter"):s.index("function sigClearFilter") + 400]
    assert "重置篩選" in s
    assert "顯示 ${sigs.length} / 共 ${_signals.length} 檔" in s
    assert "function sigClearFilter" in s
    i = s.index("function sigClearFilter")
    assert "localStorage" not in s[i:i + 400]            # 不悄悄保存檢視


def test_b09_refresh_not_disturbing():
    s = _dash()
    i = s.index("P2-B B09（更新不打擾）")
    blk = s[i:i + 700]
    assert "document.activeElement" in blk and "SELECT|BUTTON|INPUT" in blk
    assert "getSelection" in blk and "isCollapsed" in blk      # 選取文字時不打斷
    assert "ae.id!=='mw-in'" in blk                             # 搜尋框另有焦點還原、照常更新


def test_b09_last_success_time_visible():
    s = _dash()
    assert "_mw.at=new Date()" in s
    assert "最後成功更新" in s
