"""會員到價提醒 ＋ 站內通知（member_alerts / member_notes，2026-10-24）。

使用者要求「提升會員的功能」。會員替自選股設定目標價／停損價，盤中到價時自動寫一則
站內通知（會員收不到 Telegram）。這裡驗證：設定需至少一個價、到價才通知、同方向當天
只提醒一次、通知可讀取與標記已讀，以及 API 需登入。
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
    import stock_universe as su
    uni = [{"code": "2330", "name": "台積電", "market": "TSE"}]
    monkeypatch.setattr(su, "build_full_universe", lambda force_refresh=False: uni)
    return app_module.app.test_client()


def _fresh(sub):
    members.ensure_tables()
    with members._store()._conn() as conn:
        for t in ("member_alerts", "member_notes"):
            conn.execute(f"DELETE FROM {t} WHERE sub=?", (sub,))


def _wipe_all_alerts():
    # check_member_alerts() 掃「全體」會員的提醒，測試前先清掉本機 DB 其他會員殘留的提醒，
    # 否則別的測試留下的提醒也會觸發、讓計數不準。
    members.ensure_tables()
    with members._store()._conn() as conn:
        conn.execute("DELETE FROM member_alerts")
        conn.execute("DELETE FROM member_notes")


def _login(c, sub):
    members.upsert_member(sub, f"{sub}@example.com", "測試者")
    _fresh(sub)
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


H = {"X-TS": "1"}


def test_set_alert_validation_and_round_trip():
    sub = "g-al-1"
    _fresh(sub)
    assert members.set_alert(sub, "2330", "台積電", "TSE", None, None) == "need_price"   # 兩個都空
    assert members.set_alert(sub, "2330", "台積電", "TSE", "1050", None) == "ok"
    a = members.alerts_of(sub)
    assert len(a) == 1 and a[0]["code"] == "2330" and a[0]["target"] == 1050.0 and a[0]["stop"] is None


def test_check_fires_once_per_direction_per_day(monkeypatch):
    sub = "g-al-2"
    _wipe_all_alerts()
    _fresh(sub)
    members.set_alert(sub, "2330", "台積電", "TSE", "1050", "960")
    import realtime
    monkeypatch.setattr(realtime, "fetch_mis", lambda pairs: {"2330": {"code": "2330", "price": 1100.0}})   # 突破目標價
    assert members.check_member_alerts() == 1
    assert members.unread_count(sub) == 1
    assert members.check_member_alerts() == 0          # 同方向同一天不重複
    notes = members.notes_of(sub)
    assert notes and notes[0]["kind"] == "price_up" and "2330" in notes[0]["title"]


def test_check_stop_direction(monkeypatch):
    sub = "g-al-3"
    _wipe_all_alerts()
    _fresh(sub)
    members.set_alert(sub, "2330", "台積電", "TSE", None, "960")
    import realtime
    monkeypatch.setattr(realtime, "fetch_mis", lambda pairs: {"2330": {"code": "2330", "price": 900.0}})
    assert members.check_member_alerts() == 1
    assert members.notes_of(sub)[0]["kind"] == "price_down"


def test_no_fire_when_price_between(monkeypatch):
    sub = "g-al-4"
    _wipe_all_alerts()
    _fresh(sub)
    members.set_alert(sub, "2330", "台積電", "TSE", "1050", "960")
    import realtime
    monkeypatch.setattr(realtime, "fetch_mis", lambda pairs: {"2330": {"code": "2330", "price": 1000.0}})
    assert members.check_member_alerts() == 0 and members.unread_count(sub) == 0


def test_notes_read():
    sub = "g-al-5"
    _fresh(sub)
    members.add_note(sub, "price_up", "t", "b", "2330")
    assert members.unread_count(sub) == 1
    members.mark_notes_read(sub)
    assert members.unread_count(sub) == 0


def test_alerts_api_requires_login(cl):
    assert cl.get("/api/my/alerts").status_code == 401
    assert cl.get("/api/my/notifications").status_code == 401


def test_alerts_api_crud(cl):
    _login(cl, "g-al-6")
    assert cl.get("/api/my/alerts").get_json()["alerts"] == []
    r = cl.put("/api/my/alerts", json={"code": "2330", "target": "1050"}, headers=H)
    assert r.status_code == 200 and r.get_json()["alerts"][0]["target"] == 1050.0
    assert cl.put("/api/my/alerts", json={"code": "2330"}, headers=H).status_code == 400   # 無價
    assert cl.delete("/api/my/alerts?code=2330", headers=H).status_code == 200
    assert cl.get("/api/my/alerts").get_json()["alerts"] == []
