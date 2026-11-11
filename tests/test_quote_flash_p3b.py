"""美編 P3-B（brief §4 行情更新底色提示）：自選股報價 flash。

- 僅當報價「確有新值」才 flash（漲→ts-flash-up、跌→ts-flash-down，約 500ms）；首繪無前值不閃、
  同值重繪（排序/視圖切換）不閃——不輪播虛構值。
- flash keyframes 在 P3-A 的 prefers-reduced-motion:no-preference 區塊內 → reduce-motion 時自動無動作。
- 只存真實 price 快照（null 不覆蓋，避免下次誤判為變動）。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_pxflash_compares_prev_and_only_on_change():
    s = _dash()
    i = s.index("P3-B 行情更新底色提示")
    blk = s[i:i + 500]
    assert "window._mwPrevPx" in blk
    assert "r.price!==p" in blk                                  # 相等不閃
    assert "r.price>p?' ts-flash-up':' ts-flash-down'" in blk    # 漲 up、跌 down


def test_price_cell_wired():
    s = _dash()
    assert 'class="num r${pxFlash(r)}"' in s                     # 報價格套 flash 類


def test_snapshot_skips_null_price():
    s = _dash()
    i = s.index("更新報價快照供下次重繪比較")
    blk = s[i:i + 200]
    assert "if(r.price!=null)m[r.code]=r.price" in blk           # null 不覆蓋快照
    assert "window._mwPrevPx=m" in blk


def test_flash_keyframes_under_no_preference():
    s = _dash()
    # flash keyframes 必須在 no-preference 區塊（reduce-motion 不觸發）
    mo = s.index("prefers-reduced-motion:no-preference)", s.index("P3-A 動態精修"))
    end = s.index("/* ── P2-E B08", mo)
    blk = s[mo:end]
    assert "@keyframes ts-flash-up" in blk and "@keyframes ts-flash-down" in blk
