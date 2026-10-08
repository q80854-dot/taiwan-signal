"""突發狀況模擬（2026-10-08）：資料庫掛掉、官方網站回傳錯誤頁、重啟後快取全空。系統要繼續運作、不顯示假資料。"""
import pytest

import freshness as fr
import taifex as tf


def test_freshness_survives_database_outage(monkeypatch):
    from state_store import store
    def down(*a, **k):
        raise ConnectionError("database unavailable")
    monkeypatch.setattr(store, "get_meta", down)
    monkeypatch.setattr(store, "get_inst_dates", down)
    st = fr.status()
    assert {i["status"] for i in st["items"]} <= {"unknown", "late", "ok"}
    assert all(i["date"] is None or isinstance(i["date"], str) for i in st["items"])


def test_taifex_html_error_page_gives_readable_error(monkeypatch):
    class R:
        status_code, content, text, headers = 200, b"<html>maintenance</html>", "<html>maintenance</html>", {"Content-Type": "text/html"}
        def json(self):
            raise ValueError("Expecting value")
    monkeypatch.setattr(tf.requests, "get", lambda *a, **k: R())
    monkeypatch.setattr(tf.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as e:
        tf._get("/x")
    assert "非 JSON" in str(e.value) and "maintenance" in str(e.value)


def test_cold_restart_with_all_sources_down_uses_last_saved(monkeypatch):
    class S:
        d = {"taifex_last": {"futures": {"date": "2026-10-07", "items": {"外資及陸資": {"equiv_net_oi": -1}}}}}
        def get_meta(self, k, default=None): return self.d.get(k, default)
    monkeypatch.setattr(tf, "get_taifex", lambda force=False: {"errors": ["futures: down"], "stale": []})
    s = tf.summary(S())
    assert s["date"] == "2026-10-07" and "futures" in s["stale"]          # 沿用並標示，不會變成空白或假數字


def test_market_down_does_not_fake_scores(monkeypatch):
    import risk_manager as rm
    monkeypatch.setattr(rm, "check_daily_loss_limit", lambda: {})
    st = rm.get_system_status({"index": {"twii": {"source": "error"}}, "foreign": {}})
    assert st["env_score"] is None and st["can_trade"] is False
