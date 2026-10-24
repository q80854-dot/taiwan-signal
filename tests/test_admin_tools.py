"""站主系統工具 /api/admin/tools（2026-10-24）。

使用者要求「提升系統作業效率與後台工具」。這組端點讓站主一眼看到系統現況，並一鍵做
安全維運動作（清快取、重抓清單、重掃、重判休市）。這裡驗證：只有站主能用、狀態欄位齊全、
清快取動作成功、未知動作回 400、寫入動作需 X-TS。
"""
import time
import uuid

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
    return app_module.app.test_client()


H = {"X-TS": "1"}


def _login(c, email):
    sub = "u-" + uuid.uuid4().hex[:8]
    members.upsert_member(sub, email, "u")
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


def test_tools_owner_only(cl):
    _login(cl, "nobody@example.com")                 # 一般會員
    assert cl.get("/api/admin/tools").status_code == 403


def test_tools_status_shape(cl):
    _login(cl, "q80855@gmail.com")                    # 站主
    d = cl.get("/api/admin/tools").get_json()
    assert "market" in d and "cache_entries" in d and "jobs" in d and isinstance(d["jobs"], list)


def test_tools_clear_cache(cl):
    _login(cl, "q80855@gmail.com")
    r = cl.post("/api/admin/tools", json={"action": "clear_cache"}, headers=H)
    assert r.status_code == 200 and r.get_json().get("ok") is True


def test_tools_unknown_action(cl):
    _login(cl, "q80855@gmail.com")
    assert cl.post("/api/admin/tools", json={"action": "nope"}, headers=H).status_code == 400


def test_tools_needs_header(cl):
    _login(cl, "q80855@gmail.com")
    assert cl.post("/api/admin/tools", json={"action": "clear_cache"}).status_code == 403
