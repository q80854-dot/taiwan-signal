"""2026-10-15 資安稽核修補的回歸測試：M2（頻率限制取可信 IP）、L2/L4（站主專屬縱深防禦）。"""
import app as app_module
import protect
import members


def test_protect_ip_uses_rightmost_xff():
    # M2：X-Forwarded-For 最左段可偽造，必須取最右段（可信跳點）當頻率限制鍵
    with app_module.app.test_request_context(headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.1, 172.16.0.9"}):
        assert protect._ip() == "172.16.0.9"
    with app_module.app.test_request_context(headers={"X-Forwarded-For": "1.2.3.4",
                                                       "CF-Connecting-IP": "9.9.9.9"}):
        assert protect._ip() == "9.9.9.9"          # 有 Cloudflare 時仍優先


def test_app_client_ip_uses_rightmost_xff():
    with app_module.app.test_request_context(headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}):
        assert app_module._client_ip() == "203.0.113.7"


def test_owner_required_blocks_non_owner(monkeypatch):
    monkeypatch.setattr(members, "viewer_is_owner", lambda: False)

    @members.owner_required
    def view():
        return "secret"

    with app_module.app.test_request_context():
        resp = view()
        # _forbid() 回傳 (Response, 403) 形式之一：狀態碼必為 403
        status = resp.status_code if hasattr(resp, "status_code") else resp[1]
        assert status == 403


def test_owner_required_allows_owner(monkeypatch):
    monkeypatch.setattr(members, "viewer_is_owner", lambda: True)

    @members.owner_required
    def view():
        return "ok"

    with app_module.app.test_request_context():
        assert view() == "ok"
