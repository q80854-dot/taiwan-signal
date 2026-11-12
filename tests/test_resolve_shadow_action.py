"""站主維運動作「立即結算影子追蹤」（2026-11-12）。

結算原本只在盤後全市場掃描時執行（scanner 內呼叫 shadow.resolve_pending），
因此「逾期卡住」的修正要等到下次掃描才生效。新增輕量動作：只用資料庫既有
日K快取重跑結算（不對外連線、不改訊號決策），盤中亦可安全執行。
"""
import os


def _src(name):
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name),
                encoding="utf-8").read()


def test_action_wired_and_reports_real_keys():
    s = _src("app.py")
    assert 'if action == "resolve_shadow":' in s
    assert "_sh.resolve_pending()" in s
    # 回報的計數必須是 resolve_pending 真的會回傳的鍵（不得報不存在的欄位）
    import shadow
    import inspect
    body = inspect.getsource(shadow.resolve_pending)
    for key in ("rows", "closed", "unresolved", "no_new_bars", "bad_risk"):
        assert f'"{key}"' in body, f"resolve_pending 未回傳 {key}"
        assert f"_r.get('{key}'" in s, f"端點未回報 {key}"


def test_button_and_help_text_present():
    s = _src("templates/dashboard.html")
    assert "adminTool('resolve_shadow',this)" in s and "立即結算影子追蹤" in s
    assert "不對外連線、盤中可執行" in s          # 誠實說明適用時機
