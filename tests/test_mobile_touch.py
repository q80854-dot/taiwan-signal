"""手機版觸控命中與隱藏元素修正（美編 LIVE 人工複驗項落地）。

Playwright 於 390/1024/200%縮放實測：頁面層級無水平溢出（docScrollW==winW），
觸控目標修正——主要圓鈕（搜尋/通知/帳號）≥40px、小型按鈕 .btn.sm ≥38px、
次要導覽連結 .o-lk 命中區 ≥32px；並修正手機強制顯示 .acct 無視 [hidden]
造成未登入時多出幽靈通知鈕、把頂欄擠到溢出的問題。此測試鎖住 CSS 契約。
"""
import os


def _dash():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "templates", "dashboard.html")
    return open(p, encoding="utf-8").read()


def test_touch_target_fixes_present():
    s = _dash()
    # 圓鈕手機 40px（覆蓋原 34px）
    assert "html:not(.force-desktop) .srch-btn,html:not(.force-desktop) .bell,html:not(.force-desktop) .acct-btn{width:40px!important;height:40px!important}" in s
    # 小型按鈕手機 ≥38px（覆蓋原 30px）
    assert "html:not(.force-desktop) .btn.sm,html:not(.force-desktop) .btn.btn-sm{height:auto!important;min-height:38px!important" in s
    # 次要連結命中區 ≥32px
    assert "html:not(.force-desktop) .o-lk{display:inline-flex;align-items:center;min-height:32px" in s
    # 強制手機預覽同步
    assert "html.force-mobile .srch-btn,html.force-mobile .bell,html.force-mobile .acct-btn{width:40px!important;height:40px!important}" in s


def test_hidden_acct_not_forced_visible_on_mobile():
    s = _dash()
    # .acct 的手機強制顯示必須尊重 [hidden]（未登入時帳號/通知鈕不得冒出）
    assert "html:not(.force-desktop) .acct:not([hidden]){display:block!important}" in s
    assert "html.force-mobile .acct:not([hidden]){display:block!important}" in s
    # 不得再有無條件強制顯示（會顯示 hidden 的 #mnotif/#acct）
    assert "html:not(.force-desktop) .acct{display:block!important}" not in s
    assert "html.force-mobile .acct{display:block!important}" not in s
