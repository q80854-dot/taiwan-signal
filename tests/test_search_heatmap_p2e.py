"""美編 P2-E 收尾（brief §8.4 B07/B08）：熱圖圖例/缺值＋搜尋導覽功能頁分組。

- B07 熱圖：圖例（色深＝|平均漲跌幅|、滿格上限可核對）＋缺值獨立樣式（家數 0 或缺值→
  中性灰「未取得」、不以 0% 冒充）＋表格替代（指向下方排行榜）。
- B08 搜尋：全域搜尋加「功能頁」分組，站主專屬頁依 is-owner 過濾；Ctrl+K 快捷鍵不衝突
  （preventDefault）；既有側欄入口保留。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_b07_heatmap_legend_missing_table_alt():
    s = _dash()
    assert "rk-legend" in s and "滿格 ±" in s                    # 圖例含可核對上限
    assert "hx-na" in s and "未取得" in s                        # 缺值獨立樣式、不以 0% 冒充
    assert "((x.up||0)+(x.down||0))===0" in s                    # 家數 0 判定為缺值
    assert "表格替代" in s                                        # 指向排行榜表格替代
    assert "x.leader?" in s                                       # 無 leader 時不硬畫（防缺值崩）


def test_b08_search_function_pages_grouped():
    s = _dash()
    assert "GS_PAGES" in s and "function gsPages" in s and "function gsGo" in s
    assert "gs-grp" in s and "功能頁" in s and "股票" in s        # 分組標頭
    assert "classList.contains('is-owner')" in s                  # 站主頁過濾
    assert "找不到符合的股票或功能頁" in s                         # 空狀態涵蓋兩類


def test_b08_ctrlk_shortcut_no_conflict():
    s = _dash()
    i = s.index("e.key.toLowerCase()==='k'")
    assert "preventDefault" in s[i - 80:i + 80]                   # Ctrl/Cmd+K 攔截、不與瀏覽器衝突
