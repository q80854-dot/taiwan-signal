"""美編 P1-B（VISUAL_DESIGN_PLAN P1-B／brief §3 L01–L10）：未登入首頁。

- L03 文案事實：13:30 收盤、16:30 掃描＝收盤後三小時；不得再宣稱「收盤後 30 分鐘」。
- D2 主視覺：流程示意（訊號形成→風險檢核→影子追蹤），可暫停、reduce-motion 靜態、不帶績效數字。
- L05：載入中與未取得分開呈現，不以 0 或 — 裝飾缺資料。
- L08：績效區帶資料期間（由真實 equity 日期推得）。
- L10：登入 CTA 防重送（lwGo 鎖定、bfcache 返回復原）；OAuth 契約（/auth/google）不變。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_l03_headline_factual():
    s = _dash()
    assert "收盤後 30 分鐘" not in s                    # 13:30 收盤、16:30 掃描——30 分鐘不實
    assert "收盤後三小時" in s
    assert "16:30 收盤後掃描全市場" in s                # 事實標籤保留


def test_d2_flow_visual_pausable_reduced_motion():
    s = _dash()
    for t in ["lw-flow", "訊號形成", "風險檢核", "影子追蹤", "lw-fl-pause"]:
        assert t in s, t
    i = s.index(".lw-fl-seg b{")
    assert "prefers-reduced-motion:reduce){#login-wall .lw-fl-seg b{animation:none" in s
    assert "animation-play-state:paused" in s           # 可暫停
    # 主視覺不帶績效數字：流程示意區塊內無 % 或 R 數字宣稱（只驗節點標籤為文字）
    blk = s[s.index('id="lw-flow"'):s.index('id="lw-fl-pause"')]
    assert "%" not in blk and "勝率" not in blk


def test_l10_login_cta_debounce_keeps_oauth_contract():
    s = _dash()
    assert "window.lwGo" in s and "lwGo(this)" in s
    assert "location.href='/auth/google'" in s          # OAuth 入口契約不變
    assert "pageshow" in s                              # bfcache 返回復原


def test_l05_loading_vs_unavailable():
    s = _dash()
    assert s.count('<span class="ld">載入中…</span>') >= 4
    assert "未取得" in s and "lwFin" in s


def test_l08_perf_period_from_real_dates():
    s = _dash()
    assert "期間 ${String(eq[0].d" in s                 # 期間由真實 equity 日期推得，非寫死
