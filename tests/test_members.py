import base64
import json
import time

import pytest

import members
import app as app_module


@pytest.fixture()
def cl(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    members._ready["ok"] = False
    app_module.app.config["TESTING"] = True
    app_module._RATE.clear()
    import stock_universe as su
    uni = [{"code": "2330", "name": "台積電", "market": "TSE", "close": 1000.0, "change_pct": 1.0, "volume_lots": 5000, "sector": "半導體業"},
           {"code": "6488", "name": "環球晶", "market": "OTC", "close": 500.0, "change_pct": -2.0, "volume_lots": 800, "sector": "半導體業"}]
    monkeypatch.setattr(su, "build_full_universe", lambda force_refresh=False: uni)
    monkeypatch.setattr(su, "build_live_universe", lambda force_refresh=False: uni)
    return app_module.app.test_client()


def login(c, sub=None):
    import uuid
    sub = sub or "g-" + uuid.uuid4().hex[:10]       # 每次用新會員，避免本機資料庫殘留影響
    members.upsert_member(sub, f"{sub}@example.com", "測試者")
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


H = {"X-TS": "1"}


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    c = app_module.app.test_client()
    assert c.get("/api/me").get_json() == {"login_enabled": False, "user": None}
    assert c.get("/api/my/watchlist").status_code == 503
    assert c.get("/auth/google").headers["Location"].endswith("login=disabled")


def test_me_and_login_redirect(cl):
    assert cl.get("/api/me").get_json()["user"] is None
    r = cl.get("/auth/google")
    loc = r.headers["Location"]
    assert loc.startswith("https://accounts.google.com/") and "client_id=cid" in loc and "state=" in loc
    assert "ts_oauth" in r.headers.get("Set-Cookie", "")
    login(cl)
    assert cl.get("/api/me").get_json()["user"]["name"] == "測試者"


def test_watchlist_requires_login_and_header(cl):
    assert cl.get("/api/my/watchlist").status_code == 401
    login(cl)
    assert cl.post("/api/my/watchlist", json={"code": "2330"}).status_code == 403      # 缺 X-TS
    assert cl.post("/api/my/watchlist", json={"code": "2330"}, headers=H).status_code == 200


def test_watchlist_crud_and_rows(cl):
    login(cl)
    assert cl.post("/api/my/watchlist", json={"code": "2330"}, headers=H).status_code == 200
    assert cl.post("/api/my/watchlist", json={"code": "6488"}, headers=H).status_code == 200
    assert cl.post("/api/my/watchlist", json={"code": "9999"}, headers=H).status_code == 404
    assert cl.post("/api/my/watchlist", json={"code": "ab"}, headers=H).status_code == 400
    d = cl.get("/api/my/watchlist").get_json()
    assert d["grp"] == "自選股" and d["groups"][0]["count"] == 2
    row = {r["code"]: r for r in d["items"]}["2330"]
    assert row["price"] == 1000.0 and row["change"] == 9.9 and row["change_pct"] == 1.0
    assert cl.delete("/api/my/watchlist?code=2330", headers=H).status_code == 200
    assert [r["code"] for r in cl.get("/api/my/watchlist").get_json()["items"]] == ["6488"]


def test_members_are_isolated(cl):
    login(cl, "a-" + __import__("uuid").uuid4().hex[:6])
    cl.post("/api/my/watchlist", json={"code": "2330"}, headers=H)
    cl.delete_cookie(members.COOKIE)
    login(cl, "b-" + __import__("uuid").uuid4().hex[:6])
    assert cl.get("/api/my/watchlist").get_json()["items"] == []


def test_groups(cl):
    login(cl)
    assert cl.post("/api/my/groups", json={"name": "短線"}, headers=H).status_code == 200
    assert cl.post("/api/my/groups", json={"name": "短線"}, headers=H).status_code == 400     # 同名
    cl.post("/api/my/watchlist", json={"code": "2330", "grp": "短線"}, headers=H)
    d = cl.get("/api/my/watchlist?grp=短線").get_json()
    assert d["grp"] == "短線" and len(d["items"]) == 1
    assert cl.delete("/api/my/groups?name=短線", headers=H).status_code == 200
    assert cl.delete("/api/my/groups?name=自選股", headers=H).status_code == 400              # 最後一個不能刪
    assert cl.post("/api/my/watchlist", json={"code": "2330", "grp": "不存在"}, headers=H).status_code == 404


def _idt(claims):
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return b({"alg": "RS256"}) + "." + b(claims) + ".sig"


def test_verify_id_token(cl):
    ok = {"iss": "https://accounts.google.com", "aud": "cid.apps.googleusercontent.com", "exp": time.time() + 100,
          "nonce": "n1", "sub": "123", "email_verified": True}
    assert members.verify_id_token(_idt(ok), "n1")["sub"] == "123"
    assert members.verify_id_token(_idt(dict(ok, aud="other")), "n1") is None
    assert members.verify_id_token(_idt(dict(ok, exp=1)), "n1") is None
    assert members.verify_id_token(_idt(dict(ok, iss="evil.com")), "n1") is None
    assert members.verify_id_token(_idt(ok), "wrong") is None
    assert members.verify_id_token(_idt(dict(ok, email_verified=False)), "n1") is None


def test_cookie_tamper_and_callback_state(cl):
    tok = members.sign({"sub": "x", "exp": int(time.time()) + 10})
    assert members.unsign(tok)["sub"] == "x"
    assert members.unsign(tok[:-2] + "00") is None
    assert members.unsign(members.sign({"sub": "x", "exp": 1})) is None
    r = cl.get("/auth/google/callback?code=abc&state=bad")
    assert r.headers["Location"].endswith("login=failed")


def test_group_name_sanitized(cl):
    login(cl)
    cl.post("/api/my/groups", json={"name": "<b>\"短'線\">"}, headers=H)
    names = [g["name"] for g in cl.get("/api/my/watchlist").get_json()["groups"]]
    assert "b短線" in names and not any(c in n for n in names for c in "<>\"'&")


def test_login_wall(cl, monkeypatch):
    r = cl.get("/api/state")
    assert r.status_code == 401 and r.get_json()["login_required"] is True
    assert cl.get("/api/me").status_code == 200 and cl.get("/healthz").status_code == 200
    login(cl)
    assert cl.get("/api/state").status_code != 401
    monkeypatch.setenv("REQUIRE_LOGIN", "0")
    cl.delete_cookie(members.COOKIE)
    assert cl.get("/api/state").status_code != 401


def test_login_wall_off_when_disabled(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert app_module.app.test_client().get("/api/state").status_code != 401


def test_high_conf_exception():
    import risk_manager as rm
    win = [{"pnl_pct": 3.0}] * 40
    assert rm.high_conf_record(win)["perfect"] is True
    assert rm.high_conf_record(win[:29])["perfect"] is False            # 樣本不足
    assert rm.high_conf_record(win + [{"pnl_pct": -1.0}])["perfect"] is False   # 有一筆虧損就不成立
    rec = rm.high_conf_record(win)
    assert rm.high_conf_ok({"score": 95, "direction": "buy"}, rec)
    assert not rm.high_conf_ok({"score": 85, "direction": "buy"}, rec)
    assert not rm.high_conf_ok({"score": 99, "direction": "buy"}, rm.high_conf_record([]))


def test_member_roles(cl):
    import uuid
    login(cl)                                         # 一般會員（非站主）
    assert cl.get("/api/me").get_json()["user"]["owner"] is False
    for path in ("/api/scan/force", "/api/test/telegram", "/api/backfill/run", "/api/watchlist"):
        assert cl.post(path, json={}, headers=H).status_code == 403
    assert cl.get("/api/settings").status_code == 403
    assert cl.get("/api/state").status_code != 403
    assert cl.post("/api/my/watchlist", json={"code": "2330"}, headers=H).status_code == 200
    sub = "own-" + uuid.uuid4().hex[:6]               # 站主
    members.upsert_member(sub, "q80855@gmail.com", "站主")
    cl.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))
    assert cl.get("/api/me").get_json()["user"]["owner"] is True
    assert cl.get("/api/settings").status_code != 403


def test_public_pulse_and_headers(cl):
    r = cl.get("/api/public/pulse")
    assert r.status_code == 200 and "ok" in r.get_json()          # 未登入也能看（只有加權指數）
    assert cl.get("/api/state").status_code == 401                 # 其他仍要登入
    h = cl.get("/api/me").headers
    assert h["Cache-Control"] == "no-store" and "includeSubDomains" in h["Strict-Transport-Security"]
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"
    big = cl.post("/api/my/watchlist", data=b"x" * (300 * 1024), headers=H)
    assert big.status_code in (401, 413)


def _owner_login(c):
    import uuid
    sub = "own-" + uuid.uuid4().hex[:6]
    members.upsert_member(sub, "q80855@gmail.com", "站主")
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))
    return sub


def test_admin_member_stats_owner_only(cl):
    login(cl)
    assert cl.get("/api/admin/members").status_code == 403           # 一般會員看不到
    _owner_login(cl)
    d = cl.get("/api/admin/members").get_json()
    assert d["total"] >= 2 and len(d["series"]) == 30 and "members" in d and "events" in d
    assert d["active_today"] >= 1 and d["new_today"] >= 1


def test_disable_member_and_logout_all(cl):
    import uuid
    sub = "u-" + uuid.uuid4().hex[:8]
    login(cl, sub)
    assert cl.get("/api/me").get_json()["user"] is not None
    # 登出所有裝置：舊 cookie 立刻失效
    old = members.sign({"sub": sub, "exp": int(time.time()) + 3600})
    assert cl.post("/auth/logout_all", headers=H).status_code == 200
    cl.set_cookie(members.COOKIE, old)
    assert cl.get("/api/me").get_json()["user"] is None
    # 停權
    cl.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600, "v": 1}))
    assert cl.get("/api/me").get_json()["user"] is not None
    _owner_login(cl)
    assert cl.post("/api/admin/members/status", json={"sub": sub, "disabled": True}, headers=H).status_code == 200
    cl.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600, "v": 1}))
    assert cl.get("/api/me").get_json()["user"] is None
    owner_sub = _owner_login(cl)
    assert cl.post("/api/admin/members/status", json={"sub": owner_sub, "disabled": True}, headers=H).status_code == 400


def test_cross_origin_post_blocked(cl):
    login(cl)
    r = cl.post("/api/my/watchlist", json={"code": "2330"}, headers=dict(H, Origin="https://evil.example"))
    assert r.status_code == 403
    r = cl.post("/api/my/watchlist", json={"code": "2330"}, headers=dict(H, Origin="http://localhost"))
    assert r.status_code == 200


def test_auth_rate_limit(cl):
    members._AUTH_RATE.clear()
    codes = [cl.get("/auth/google").status_code for _ in range(32)]
    assert codes[0] == 302 and "login=busy" in cl.get("/auth/google").headers["Location"]
    members._AUTH_RATE.clear()


def test_premarket_mode_flag(monkeypatch):
    from scanner import scanner
    seen = {}
    monkeypatch.setattr(scanner, "_run_daily_scan_impl", lambda: seen.setdefault("m", scanner._mode))
    scanner.run_daily_scan(mode="premarket")
    assert seen["m"] == "premarket" and scanner._mode == "close"
    scanner.run_daily_scan(mode="weird")
    assert scanner._mode == "close"


# ───────── protect.py：湧入防護 ─────────
def test_protect_param_checks_and_cache(cl):
    import protect
    login(cl)
    protect._rate.clear(); protect._heavy_rate.clear(); protect.clear_cache()
    assert cl.get("/api/instruments/..%2Fetc").status_code in (400, 404)
    assert cl.get("/api/instruments/AB%20CD").status_code == 400
    assert cl.get("/api/signals?limit=abc").status_code == 400
    assert cl.get("/api/signals?limit=99999").status_code == 400
    r1 = cl.get("/api/signals?limit=5")
    r2 = cl.get("/api/signals?limit=5")
    assert r1.status_code == 200 and r2.headers.get("X-Cache") == "HIT" and r1.get_data() == r2.get_data()
    # 個人資料絕不快取
    cl.get("/api/my/watchlist"); assert cl.get("/api/my/watchlist").headers.get("X-Cache") is None


def test_protect_rate_limit_before_login():
    import protect
    from app import app
    c = app.test_client()
    protect._rate.clear()
    codes = [c.get("/api/signals").status_code for _ in range(protect.RATE_LIMIT + 5)]
    assert codes[-1] == 429                           # 未登入、登入牆有沒有開，都會被限流
    protect._rate.clear()


def test_protect_5xx_sanitized():
    from app import app
    from flask import jsonify
    import protect
    with app.test_request_context("/api/whatever"):
        r = jsonify({"error": "secret db password xyz"})
        r.status_code = 500
        out = protect.after(r)
        assert out.status_code == 500 and "secret" not in out.get_data(as_text=True)


def test_dashboard_style_tags_balanced():
    import os
    s = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html"), encoding="utf-8").read()
    assert s.count("<style") == s.count("</style>")
    assert 'id="tabbar"' in s and 'id="acct-menu"' in s


def test_member_cannot_see_capital_fields_but_owner_can(cl, monkeypatch):
    import protect
    from state_store import store
    rows = [{"id": 1, "ticker": "2330.TW", "code": "2330", "name": "台積電", "direction": "buy", "result": "pending",
             "status": "active", "entry_price": 100, "stop_loss": 95, "suggested_lots": 7, "risk_twd": 35000,
             "risk_pct": 1.0, "pnl_twd": 1234, "generated_at": "2026-10-08T08:30:00"}]
    monkeypatch.setattr(store, "get_recent_signals", lambda limit=60, days_back=30: rows)
    monkeypatch.setattr(app_module, "_bars_since", lambda t, g: ({}, []))
    login(cl)
    j = cl.get("/api/positions").get_json()
    p = j["open"][0]
    assert "risk_twd" not in p and "lots" not in p and "pnl_twd" not in p and p["risk_pct"] == 1.0
    # 站主（同一路徑剛被會員版快取過）仍拿到完整欄位：快取依身分分開
    monkeypatch.setenv("OWNER_EMAILS", "boss@example.com")
    members.upsert_member("boss", "boss@example.com", "站主")
    cl.set_cookie(members.COOKIE, members.sign({"sub": "boss", "exp": int(time.time()) + 3600}))
    p2 = cl.get("/api/positions").get_json()["open"][0]
    assert p2["risk_twd"] == 35000 and p2["lots"] == 7
    protect.clear_cache()


def test_probing_ip_gets_temporarily_banned(cl, monkeypatch):
    import protect
    monkeypatch.setattr(protect, "BAN_DENIES", 5)
    h = {"CF-Connecting-IP": "203.0.113.9"}
    for _ in range(10):
        assert cl.get("/api/me", headers=h).status_code == 200      # 正常請求不計
    for _ in range(6):
        cl.get("/api/instruments/<script>", headers=h)               # 代號格式錯誤 → 400
    assert cl.get("/api/me", headers=h).status_code == 429           # 已封鎖
    assert cl.get("/api/me", headers={"CF-Connecting-IP": "198.51.100.1"}).status_code == 200   # 其他人不受影響
    for _ in range(20):
        cl.get("/api/state", headers={"CF-Connecting-IP": "198.51.100.2"})                       # 未登入 401 不會被封
    assert cl.get("/api/me", headers={"CF-Connecting-IP": "198.51.100.2"}).status_code == 200
