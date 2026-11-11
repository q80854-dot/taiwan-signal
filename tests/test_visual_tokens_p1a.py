"""美編 P1-A（VISUAL_DESIGN_PLAN §2/P1-A）：設計系統 token 層。

- 單一具名樣式區 ts-tokens 存在、位於歷史 skin 層（skin-v4）之後（cascade 收斂點）。
- 間距/字級/圓角 token 齊備；語意色三組為 alias（隨主題切換、不寫死色票）。
- 資料五態基礎元件（.st 系）齊備；不動既有 .empty/.skel。
"""
import os
import re


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_tokens_block_exists_after_skins():
    s = _dash()
    i_v4 = s.index('<style id="skin-v4">')
    i_tok = s.index('<style id="ts-tokens">')
    assert i_tok > i_v4                                        # token 層在最後一個 skin 之後（後者勝）
    head_end = s.index("</head>")
    assert i_tok < head_end                                    # 仍在 head 內（靜態載入，非 JS 插入）


def test_spacing_type_radius_tokens():
    s = _dash()
    blk = s[s.index('<style id="ts-tokens">'):s.index("</style>", s.index('<style id="ts-tokens">'))]
    for t in ["--sp-1:4px", "--sp-8:64px", "--fs-body:14px", "--fs-page:24px",
              "--rad-show:20px", "--rad-work:10px", "--rad-pill:999px"]:
        assert t in blk, t


def test_semantic_colors_are_theme_aliases():
    s = _dash()
    blk = s[s.index('<style id="ts-tokens">'):s.index("</style>", s.index('<style id="ts-tokens">'))]
    # 三組語意色皆 alias 既有主題 token，不得在本層寫死色票（隨 data-theme 自動切換）
    for t in ["--c-brand:var(--acc)", "--c-up:var(--up)", "--c-down:var(--down)",
              "--c-ok:var(--ok)", "--c-warn:var(--warn)", "--c-alert:var(--alert)",
              "--c-unknown:var(--t3)"]:
        assert t in blk, t
    assert not re.search(r"--c-(brand|up|down|ok|warn|alert)[^:]*:\s*#", blk)   # 無硬編 hex


def test_five_state_components():
    s = _dash()
    blk = s[s.index('<style id="ts-tokens">'):s.index("</style>", s.index('<style id="ts-tokens">'))]
    for cls in [".st.st-error", ".st.st-stale", ".st.st-forbidden", ".st.st-loading"]:
        assert cls in blk, cls
    assert "prefers-reduced-motion" in blk                     # loading 動態尊重 reduce-motion
    # 既有元件未被刪除
    assert ".empty{" in s and ".skel" in s
