"""攻擊模擬（2026-10-08）：以外部測試者／攻擊者的角度實際打一遍，確認每一道防線都擋得住。

每個測試名稱就是一種攻擊情境；任何一個失敗＝有漏洞。CI 每次都會跑，之後改程式不會不小心把防線拆掉。
"""
import json
import time

import pytest

import app as app_module
import members
import protect

OPEN_OK = {"/api/me", "/api/public/pulse"}             # 未登入本來就可以看的
H = {"X-TS": "1"}


@pytest.fixture()
def cl(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    monkeypatch.setenv("OWNER_EMAILS", "boss@example.com")
    members._ready["ok"] = False
    app_module.app.config["TESTING"] = True
    app_module._RATE.clear()
    return app_module.app.test_client()


def _login(c, sub="tester", email="tester@example.com"):
    members.upsert_member(sub, email, "測試者")
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


def _api_get_routes():
    out = []
    for r in app_module.app.url_map.iter_rules():
        p = r.rule
        if not p.startswith("/api/") or "GET" not in r.methods:
            continue
        p = p.replace("<ticker>", "2330").replace("<code>", "2330").replace("<key>", "x")
        if "<" in p:
            continue
        out.append(p)
    return sorted(set(out))


def _api_write_routes():
    out = []
    for r in app_module.app.url_map.iter_rules():
        if not r.rule.startswith("/api/") or "<" in r.rule:
            continue
        for m in ("POST", "DELETE", "PUT"):
            if m in r.methods:
                out.append((m, r.rule))
    return out


# ── 1. 未登入：除了少數公開端點，全部 401 ──
def test_anonymous_cannot_read_any_private_api(cl, monkeypatch):
    leaks = []
    for p in _api_get_routes():
        if p in OPEN_OK or p.startswith("/api/public/"):
            continue
        r = cl.get(p)
        if r.status_code not in (401, 403, 429):
            leaks.append((p, r.status_code))
    assert not leaks, f"未登入可讀取：{leaks}"


# ── 2. 一般會員：不能讀站主後台、不能做任何寫入（除了自己的自選股）──
OWNER_ONLY_GET = ["/api/health", "/api/settings", "/api/audit", "/api/diagnostics", "/api/admin/members",
                  "/api/admin/shadow", "/api/learning", "/api/backtest/full/result", "/api/backfill/result"]


def test_member_cannot_read_owner_pages(cl):
    _login(cl)
    bad = [(p, cl.get(p).status_code) for p in OWNER_ONLY_GET if cl.get(p).status_code != 403]
    assert not bad, f"一般會員可讀站主頁：{bad}"


def test_member_cannot_trigger_owner_actions(cl):
    _login(cl)
    bad = []
    for m, p in _api_write_routes():
        if p.startswith("/api/my/"):
            continue
        r = cl.open(p, method=m, json={}, headers=H)
        if r.status_code not in (401, 403, 429):
            bad.append((m, p, r.status_code))
    assert not bad, f"一般會員可觸發：{bad}"


# ── 3. 偽造／竄改登入憑證 ──
def test_forged_or_tampered_cookie_rejected(cl):
    good = members.sign({"sub": "boss", "exp": int(time.time()) + 3600})
    body, sig = good.split(".")
    for tok in (body + "." + "0" * len(sig),                                  # 改簽章
                members.sign({"sub": "boss", "exp": int(time.time()) - 10}),  # 已過期
                "eyJzdWIiOiJib3NzIn0.",                                       # 沒簽章
                "garbage"):
        cl.set_cookie(members.COOKIE, tok)
        assert cl.get("/api/state").status_code == 401, tok


def test_member_cannot_escalate_by_claiming_owner_email(cl):
    # 會員資料裡的 email 由 Google 驗證後寫入；cookie 只帶 sub，竄改不了 email
    _login(cl, sub="mallory", email="mallory@example.com")
    assert cl.get("/api/settings").status_code == 403
    assert cl.get("/api/me").get_json()["user"]["owner"] is False


# ── 4. 跨站請求偽造（CSRF）──
def test_cross_site_write_blocked(cl):
    _login(cl)
    r = cl.post("/api/my/watchlist", json={"code": "2330"}, headers={"X-TS": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert cl.post("/api/my/watchlist", json={"code": "2330"}).status_code == 403          # 少了自訂標頭也擋


# ── 5. 注入／路徑穿越／怪異參數 ──
@pytest.mark.parametrize("bad", ["2330';DROP TABLE x;--", "..%2F..%2Fetc%2Fpasswd", "<script>alert(1)</script>",
                                 "A" * 50, "2330.TW.EXE"])
def test_bad_ticker_rejected(cl, bad):
    _login(cl)
    assert cl.get("/api/instruments/" + bad).status_code in (400, 404)


def test_limit_param_bounds(cl):
    _login(cl)
    for v in ("0", "-1", "999999", "abc", "1e9"):
        assert cl.get(f"/api/events?limit={v}").status_code == 400


def test_stored_xss_payload_is_data_not_markup(cl):
    _login(cl)
    payload = '<img src=x onerror=alert(1)>'
    cl.post("/api/my/groups", json={"name": payload}, headers=H)
    r = cl.get("/api/my/watchlist")
    assert r.headers["Content-Type"].startswith("application/json")       # JSON 不會被瀏覽器當 HTML 執行
    html = app_module.app.test_client().get("/").get_data(as_text=True)
    assert "const escH=" in html and "safe=(v,fb='—')=>(v===null||v===undefined||v===''||v!==v)?fb:(typeof v==='string'?escH(v):v)" in html


# ── 6. 超大請求／灌流量 ──
def test_oversized_body_rejected(cl):
    _login(cl)
    r = cl.post("/api/my/groups", data="x" * (300 * 1024), headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 413


def test_flood_is_rate_limited(cl, monkeypatch):
    monkeypatch.setattr(protect, "RATE_LIMIT", 30)
    h = {"CF-Connecting-IP": "203.0.113.50"}
    codes = [cl.get("/api/me", headers=h).status_code for _ in range(40)]
    assert 429 in codes and codes[0] == 200


# ── 7. 錯誤訊息不外洩內部細節 ──
def test_server_errors_do_not_leak_internals(cl, monkeypatch):
    _login(cl)
    import stock_universe
    def boom(*a, **k):
        raise RuntimeError("postgres://user:secretpw@internal-host:5432/db")
    monkeypatch.setattr(stock_universe, "get_market_breadth", boom)
    r = cl.get("/api/market/breadth")
    assert r.status_code == 500 and "secretpw" not in r.get_data(as_text=True)


# ── 7b. 資料庫斷線／連線耗盡：即使端點用 200 夾帶 str(e)，也不得外洩 socket 路徑、連線字串、驅動程式錯誤 ──
_LEAK_MARKERS = ["postgres://", "postgresql://", "psycopg2", "Traceback", ".s.PGSQL",
                 "/home/", "/usr/", "/var/", "could not connect", "connection to server", "password"]


def test_db_outage_does_not_leak_internals_even_on_200(cl, monkeypatch):
    # 模擬 Render Postgres 短暫斷線：連線時丟出帶 socket 路徑與連線字串的原始錯誤
    _login(cl)
    import state_store
    msg = ('connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: '
           'FATAL password authentication failed; dsn=postgres://u:pw@host:5432/db')

    def down(*a, **k):
        raise RuntimeError(msg)
    monkeypatch.setattr(state_store.store, "_conn", down)
    for path in ("/api/db_stats", "/api/instruments/2330", "/api/market_extras"):
        body = cl.get(path).get_data(as_text=True)
        hit = [m for m in _LEAK_MARKERS if m in body]
        assert not hit, f"{path} 外洩內部細節：{hit}"


def test_redactor_keeps_user_messages(cl):
    import protect
    assert protect._redact({"error": "請選擇回報類別"}) == {"error": "請選擇回報類別"}      # 中文提示照常
    assert protect._redact({"error": 'psycopg2 OperationalError: /var/x'})["error"] == protect._SAFE_ERR
    assert protect._redact({"a": [{"error": "host=db password=x"}]})["a"][0]["error"] == protect._SAFE_ERR


# ── 8. 安全標頭 ──
def test_security_headers_present(cl):
    r = cl.get("/")
    for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Strict-Transport-Security", "Referrer-Policy"):
        assert h in r.headers, h
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
    assert cl.get("/api/me").headers.get("Cache-Control") == "no-store"


# ── 9. 會員名單（含 email）只有站主看得到 ──
def test_member_list_not_leaked(cl):
    _login(cl)
    r = cl.get("/api/admin/members")
    assert r.status_code == 403 and "@example.com" not in r.get_data(as_text=True)


# ── 10. Telegram webhook：沒設定 token 時不能被任何人當成開放端點 ──
def test_webhook_without_token_is_closed(cl, monkeypatch):
    r = cl.post("/webhook/", json={"message": {"chat": {"id": 1}, "text": "/start"}})
    assert r.status_code in (403, 404, 405)
