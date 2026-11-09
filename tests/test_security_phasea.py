"""Phase A 資安硬化（2026-11-09，依第二階段審查第八部分開發順序）。

鎖定三項查證屬實的 fail-open / 可偽造問題的修正：
1) gate() 認證例外時 fail-closed（受保護 /api/ → 503，不再無聲放行）。
2) _ip() 取 X-Forwarded-For 最右段（可信代理附上），不取可偽造的最左段。
3) viewer_is_owner 單人模式可用 OWNER_FAIL_OPEN=0 改為 fail-closed。
"""
import members
import app as app_module


def test_ip_uses_rightmost_xff_not_leftmost():
    with app_module.app.test_request_context(headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 9.9.9.9"}):
        assert members._ip() == "9.9.9.9"          # 最右段（可信代理附上）
    # CF-Connecting-IP 優先
    with app_module.app.test_request_context(headers={"CF-Connecting-IP": "8.8.8.8",
                                                       "X-Forwarded-For": "1.1.1.1"}):
        assert members._ip() == "8.8.8.8"


def test_owner_fail_closed_opt_in(monkeypatch):
    # 未設定登入（enabled()=False）→ 預設 OWNER_FAIL_OPEN 未設＝視為站主（維持現狀、不鎖人）
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("OWNER_FAIL_OPEN", raising=False)
    members._ready["ok"] = False
    assert members.viewer_is_owner() is True
    # 明確設 OWNER_FAIL_OPEN=0 → fail-closed（非站主）
    monkeypatch.setenv("OWNER_FAIL_OPEN", "0")
    assert members.viewer_is_owner() is False


def test_gate_fail_closed_on_auth_exception(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    monkeypatch.setenv("REQUIRE_LOGIN", "1")
    monkeypatch.delenv("OWNER_FAIL_OPEN", raising=False)
    members._ready["ok"] = False

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(members, "current_member", _boom)

    with app_module.app.test_request_context("/api/settings", method="GET"):
        resp = members.gate()
    assert resp is not None                          # 不再 fail-open（原為 None=放行）
    assert resp.status_code == 503                    # 受保護 API 擋下


def test_gate_fail_closed_allows_open_paths(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    monkeypatch.setenv("REQUIRE_LOGIN", "1")
    members._ready["ok"] = False
    monkeypatch.setattr(members, "current_member", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    # 開放路徑（/api/me）即使例外也不應擋（靜態/公開不受影響）
    with app_module.app.test_request_context("/api/me", method="GET"):
        resp = members.gate()
    assert resp is None
