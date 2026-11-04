"""通知偏好細化（2026-11-04）：逐檔推播依最低等級/方向/只自選過濾。"""
import state_store
from scanner import scanner


def _sigs():
    return [{"code": "2330", "grade": "A", "direction": "buy"},
            {"code": "2317", "grade": "B", "direction": "buy"},
            {"code": "2603", "grade": "C", "direction": "sell"}]


def _set_prefs(monkeypatch, prefs):
    monkeypatch.setattr(state_store.store, "get_meta",
                        lambda k, d=None: prefs if k == "notify_prefs" else d)


def test_min_grade_filter(monkeypatch):
    _set_prefs(monkeypatch, {"min_grade": "B", "direction": "all", "watchlist_only": False})
    assert {s["code"] for s in scanner._apply_notify_prefs(_sigs())} == {"2330", "2317"}


def test_direction_filter(monkeypatch):
    _set_prefs(monkeypatch, {"min_grade": "C", "direction": "sell", "watchlist_only": False})
    assert {s["code"] for s in scanner._apply_notify_prefs(_sigs())} == {"2603"}


def test_watchlist_only(monkeypatch):
    import members
    _set_prefs(monkeypatch, {"min_grade": "C", "direction": "all", "watchlist_only": True})
    monkeypatch.setattr(members, "owner_watch_codes", lambda: {"2317"})
    assert {s["code"] for s in scanner._apply_notify_prefs(_sigs())} == {"2317"}


def test_no_prefs_pushes_all(monkeypatch):
    _set_prefs(monkeypatch, None)
    assert len(scanner._apply_notify_prefs(_sigs())) == 3
