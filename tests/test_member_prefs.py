"""會員個人偏好雲端化（member_prefs，2026-10-24）。

使用者要求「提升會員的功能和設定可調整的東西」。看盤樣式／排序／主題原本只存瀏覽器，
這裡驗證存進帳號後可跨裝置讀回：需登入、只收白名單鍵、值做清洗、PUT 為合併更新。
"""
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
    return app_module.app.test_client()


def _login(c, sub):
    members.upsert_member(sub, f"{sub}@example.com", "測試者")
    with members._store()._conn() as conn:           # 清掉本機 DB 可能殘留的偏好，確保乾淨起點
        conn.execute("DELETE FROM member_prefs WHERE sub=?", (sub,))
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


H = {"X-TS": "1"}


def test_prefs_require_login(cl):
    assert cl.get("/api/my/prefs").status_code == 401
    assert cl.put("/api/my/prefs", json={"prefs": {"view": "chips"}}).status_code == 401


def test_prefs_round_trip_and_merge(cl):
    sub = "g-prefs-1"
    _login(cl, sub)
    assert cl.get("/api/my/prefs").get_json()["prefs"] == {}                    # 預設為空
    r = cl.put("/api/my/prefs", json={"prefs": {"view": "chips", "sort": {"k": "close", "d": -1}, "theme": "dark"}}, headers=H)
    p = r.get_json()["prefs"]
    assert p["view"] == "chips" and p["sort"] == {"k": "close", "d": -1} and p["theme"] == "dark"
    assert cl.get("/api/my/prefs").get_json()["prefs"]["view"] == "chips"       # 讀回一致
    p2 = cl.put("/api/my/prefs", json={"prefs": {"view": "basic"}}, headers=H).get_json()["prefs"]
    assert p2["view"] == "basic" and p2["theme"] == "dark"                      # PUT 為合併：theme 保留


def test_prefs_sanitized(cl):
    _login(cl, "g-prefs-2")
    p = cl.put("/api/my/prefs", json={"prefs": {
        "view": "quote",
        "evil": "should be dropped",            # 非白名單鍵 → 丟棄
        "theme": "x" * 100,                      # 過長字串 → 截斷到 40
        "sort": {"k": "close;x", "d": 9},        # 欄位名清掉非法字元（; 被移除）、d 正規化
    }}, headers=H).get_json()["prefs"]
    assert "evil" not in p
    assert len(p["theme"]) == 40
    assert p["sort"]["k"] == "closex" and p["sort"]["d"] == 1   # ; 清掉；非 -1 一律視為 1


def test_prefs_bad_body_rejected(cl):
    _login(cl, "g-prefs-3")
    assert cl.put("/api/my/prefs", json={"prefs": "notadict"}, headers=H).status_code == 400


def test_ensure_tables_runs_alter_in_separate_transaction(monkeypatch):
    """迴歸：2026-10-24 production bug——ensure_tables 的 ALTER 若和 CREATE 共用同一個交易，
    Postgres 在 ALTER 失敗（欄位已存在）時會把整個交易 abort、commit 等同回滾，連新表的
    CREATE 一起消失（SQLite 不會，所以本機測不出）。這裡結構性驗證：建表與 ALTER 分屬不同
    _conn() 交易（至少開兩次連線），且新表建立後可正常查詢。"""
    from state_store import store
    members._ready["ok"] = False
    orig = store._conn
    n = {"c": 0}

    def counting():
        n["c"] += 1
        return orig()

    monkeypatch.setattr(store, "_conn", counting)
    members.ensure_tables()
    assert n["c"] >= 2, "CREATE 與 ALTER 必須分開交易，否則 Postgres 會連帶回滾新表"
    # 新表可用（不會 UndefinedTable）
    assert members.get_prefs("nobody-x") == {}
    assert members.notes_of("nobody-x") == []
    assert members.alerts_of("nobody-x") == []
