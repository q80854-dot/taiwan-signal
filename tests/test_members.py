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
