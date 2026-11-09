"""Telegram 逐收件者對帳（Phase C，2026-11-09，依第二階段審查 §五）。

審查點名：目前只要任一頻道或任一管理員成功，訊號就被記為送達，站主本人私訊失敗會被掩蓋。
本測試鎖定 push_signal 現在單獨回報『站主本人是否收到』（owner_ok），與 any_ok 分離。
"""
import telegram_bot as tb


def _setup(monkeypatch, owner_id, admin_list, send_fn,
           free_channel="freechan", paid_channel=""):
    monkeypatch.setattr(tb, "format_signal_paid", lambda sig: "MSG")
    monkeypatch.setattr(tb, "TELEGRAM_CHAT_ID", owner_id)
    monkeypatch.setattr(tb, "TELEGRAM_FREE_CHANNEL", free_channel)
    monkeypatch.setattr(tb, "TELEGRAM_PAID_CHANNEL", paid_channel)
    monkeypatch.setattr(tb, "_load_subscribers",
                        lambda: {"free": [], "paid": [], "admin": list(admin_list)})
    monkeypatch.setattr(tb, "send_message", send_fn)
    monkeypatch.setattr(tb.time, "sleep", lambda *a, **k: None)


_SIG = {"name": "測試股", "direction": "buy", "score": 80}


def test_owner_miss_not_masked_by_channel(monkeypatch):
    # 站主本人失敗，但免費頻道與另一位 admin 成功 → any_ok 真、owner_ok 假（不被掩蓋）。
    def send(chat_id, text, parse_mode="HTML", _max_retries=3):
        return str(chat_id) != "owner1"
    _setup(monkeypatch, "owner1", ["owner1", "admin2"], send)
    res = tb.push_signal(_SIG)
    assert res["any_ok"] is True
    assert res["owner_configured"] is True
    assert res["owner_ok"] is False


def test_owner_ok_when_owner_receives(monkeypatch):
    # 全部成功 → owner_ok 真。
    def send(chat_id, text, parse_mode="HTML", _max_retries=3):
        return True
    _setup(monkeypatch, "owner1", ["owner1", "admin2"], send)
    res = tb.push_signal(_SIG)
    assert res["any_ok"] is True
    assert res["owner_ok"] is True


def test_owner_not_configured(monkeypatch):
    # 沒設站主 chat → owner_configured 假（scanner 端不會誤發站主漏收告警）。
    def send(chat_id, text, parse_mode="HTML", _max_retries=3):
        return True
    _setup(monkeypatch, "", [], send)
    res = tb.push_signal(_SIG)
    assert res["owner_configured"] is False
    assert res["owner_ok"] is False


def test_owner_fallback_send_when_not_in_admin(monkeypatch):
    # 站主有設定但不在 admin 清單（異常情況）→ 補送一次，本人成功則 owner_ok 真。
    calls = []
    def send(chat_id, text, parse_mode="HTML", _max_retries=3):
        calls.append(str(chat_id))
        return True
    _setup(monkeypatch, "ownerX", ["admin2"], send)   # ownerX 不在 admin
    res = tb.push_signal(_SIG)
    assert res["owner_ok"] is True
    assert "ownerX" in calls                            # 確實補送給本人
