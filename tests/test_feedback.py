import time

import pytest

import app as app_module
import feedback
import members

H = {"X-TS": "1"}


@pytest.fixture()
def cl(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    monkeypatch.setenv("OWNER_EMAILS", "boss@example.com")
    members._ready["ok"] = False
    feedback._ready["ok"] = False
    monkeypatch.setattr(feedback, "_notify", lambda *a, **k: None)
    app_module.app.config["TESTING"] = True
    app_module._RATE.clear()
    return app_module.app.test_client()


def _login(c, sub, email):
    members.upsert_member(sub, email, sub)
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


def test_anonymous_cannot_submit(cl):
    assert cl.post("/api/my/feedback", json={"category": "bug", "message": "壞掉了啦"}, headers=H).status_code == 401


def test_member_submit_validation_and_list(cl):
    import uuid
    sub = "fb-" + uuid.uuid4().hex[:8]
    _login(cl, sub, f"{sub}@example.com")
    assert cl.post("/api/my/feedback", json={"category": "bug", "message": "壞掉了"}).status_code == 403   # 缺 X-TS
    assert cl.post("/api/my/feedback", json={"category": "x", "message": "內容夠長了"}, headers=H).status_code == 400
    assert cl.post("/api/my/feedback", json={"category": "bug", "message": "短"}, headers=H).status_code == 400
    assert cl.post("/api/my/feedback", json={"category": "bug", "message": "x" * 2001}, headers=H).status_code == 400
    xss = '<img src=x onerror=alert(1)> 頁面跑版'
    r = cl.post("/api/my/feedback", json={"category": "bug", "message": xss, "page": "positions",
                                          "ctx": {"ua": "iPhone", "vw": 393, "vh": "abc"}}, headers=H)
    assert r.status_code == 200
    items = cl.get("/api/my/feedback").get_json()["items"]
    assert items[0]["message"] == xss and items[0]["status"] == "open" and "email" not in items[0]


def test_rate_limit_per_hour(cl):
    import uuid
    sub = "fb-" + uuid.uuid4().hex[:8]
    _login(cl, sub, f"{sub}@example.com")
    codes = [cl.post("/api/my/feedback", json={"category": "idea", "message": f"建議第 {i} 則"}, headers=H).status_code for i in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_owner_manage_and_member_forbidden(cl):
    import uuid
    sub = "fb-" + uuid.uuid4().hex[:8]
    _login(cl, sub, f"{sub}@example.com")
    fid = cl.post("/api/my/feedback", json={"category": "data", "message": "投信數字怪怪的"}, headers=H).get_json()["id"]
    assert cl.get("/api/admin/feedback").status_code == 403
    assert cl.post("/api/admin/feedback/status", json={"id": fid, "status": "done"}, headers=H).status_code == 403
    _login(cl, "boss", "boss@example.com")
    lst = cl.get("/api/admin/feedback").get_json()
    row = next(x for x in lst["items"] if x["id"] == fid)
    assert row["email"] == f"{sub}@example.com" and lst["counts"]["open"] >= 1
    assert cl.post("/api/admin/feedback/status", json={"id": fid, "status": "done", "note": "已修正"}, headers=H).status_code == 200
    assert cl.post("/api/admin/feedback/status", json={"id": "../x", "status": "done"}, headers=H).status_code == 400
    _login(cl, sub, f"{sub}@example.com")
    mine = cl.get("/api/my/feedback").get_json()["items"]
    assert next(x for x in mine if x["id"] == fid)["status_zh"] == "已處理"
