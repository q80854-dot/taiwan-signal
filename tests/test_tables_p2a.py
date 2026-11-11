"""美編 P2-A（brief §8.4 B01/B02）：資訊密度切換＋表格排序可及性與固定欄。

- B01：compact/舒適兩種密度——只縮表格/列間距（主要閱讀文字維持 14px 下限）、
  偏好存本機 localStorage（try/catch）、不影響任何模型參數；開機早期腳本即套用避免閃動。
- B02：自選股可排序欄帶 aria-sort/scope；水平捲動交界固定識別欄（第一欄 sticky）；
  捲動容器內固定欄頭；總筆數可見。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_b01_density_local_pref_and_early_apply():
    s = _dash()
    assert "dashDensity" in s and "density-compact" in s
    # 開機早期腳本套用（避免表格先寬後窄閃動）＋ try/catch
    head = s[:s.index("</head>")]
    assert "localStorage.getItem('dashDensity')" in head
    assert "function toggleDensity" in s and "localStorage.setItem('dashDensity'" in s
    assert 'id="density-btn"' in s and "aria-pressed" in s


def test_b01_compact_only_shrinks_spacing_not_body_font():
    s = _dash()
    i = s.index("density-compact .tbl td")
    blk = s[i - 200:i + 600]
    assert "padding" in blk
    assert "font-size" not in blk                       # 只縮間距、不縮內文字級


def test_b02_aria_sort_and_sticky():
    s = _dash()
    assert 'aria-sort="${_mw.sort.k===c?' in s          # 排序方向可及
    assert 'scope="col"' in s
    assert ".mw-tbl th:first-child,.mw-tbl td:first-child{position:sticky;left:0" in s   # 識別欄固定
    assert ".lrn-clamp thead th{position:sticky;top:0" in s                              # 容器內固定欄頭
    assert "共 ${rows.length} 檔" in s                   # 總筆數
