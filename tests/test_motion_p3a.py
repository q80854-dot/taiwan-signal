"""美編 P3-A（brief §4 動態規格）：動態精修基礎層。

- 動態 token（時長）定義於 ts-tokens；全部動態包在 prefers-reduced-motion:no-preference，
  且整站已有全域 reduce kill（*{animation:none;transition:none}）→ reduce-motion 一律無動作。
- CTA 按壓回饋（translateY 1px、hit area 不縮小）；互動卡 hover opt-in（.ts-hover，hover:hover 守門）；
  行情更新 flash keyframes（備 P3-B，僅確有新值時加類）。
- 禁止清單合規：無連續 requestAnimationFrame（唯一 rAF 為一次性 layout 修正）。
"""
import os
import re


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_motion_tokens_defined():
    s = _dash()
    for t in ["--mo-press:", "--mo-hover:", "--mo-tab:", "--mo-flash:500ms", "--mo-ease:"]:
        assert t in s, t


def test_global_reduce_motion_kill_present():
    s = _dash()
    # 全域 reduce-motion 關閉開關（整站尊重）
    assert re.search(r"@media\(prefers-reduced-motion:reduce\)\{\*\{animation:none!important;transition:none!important\}\}", s)


def test_cta_press_no_hit_area_shrink():
    s = _dash()
    # 取實際 CSS 規則（: active 起到該規則收尾），不含註解
    i = s.index("html[data-theme] .btn:active")
    rule = s[i:s.index("}", i) + 1]
    assert "translateY(1px)" in rule and "transition:transform" in rule
    assert "padding" not in rule and "width" not in rule and "height" not in rule   # 只 translate，不縮 hit area


def test_interactive_card_hover_opt_in_and_guarded():
    s = _dash()
    assert ".ts-hover" in s and "hover:hover" in s              # 僅 opt-in＋hover 裝置守門（觸控不依賴）
    i = s.index(".ts-hover:hover")
    assert "translateY(-2px)" in s[i:i + 60]


def test_quote_flash_keyframes_ready_under_no_preference():
    s = _dash()
    # flash 動態在 no-preference 區塊內（reduce-motion 時不觸發）
    mo = s.index("prefers-reduced-motion:no-preference)", s.index("P3-A 動態精修"))
    blk = s[mo:mo + 1200]
    assert "ts-flash-up" in blk and "ts-flash-down" in blk
    assert "var(--c-up-soft)" in blk and "var(--c-down-soft)" in blk


def test_only_oneshot_raf():
    s = _dash()
    # 禁止清單：不得有連續 rAF 迴圈——全檔 requestAnimationFrame 僅 1 處且為一次性 layout 修正
    assert s.count("requestAnimationFrame") == 1
