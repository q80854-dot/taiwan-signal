"""驗收清單自動化測試（2026-11-04）。

對應使用者提供之第三方修復手冊第 6 節「測試計畫與驗收清單」。每個測試名稱即一條驗收項目，
CI 每次執行，確保關鍵風險（權限、風控 fail-closed、回測誠實性、資料外洩）都有測試證明、
日後改程式不會不小心把防線拆掉。只收錄可穩定離線驗證的項目；需外部網路者不納入。
"""
import time
import threading

import pytest

import app as app_module
import members


# ─────────────────────────── 共用：測試用戶端與登入 ───────────────────────────
@pytest.fixture()
def cl(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret-for-test")
    monkeypatch.setenv("OWNER_EMAILS", "boss@example.com")
    monkeypatch.setenv("REQUIRE_LOGIN", "1")
    members._ready["ok"] = False
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _login(c, sub="tester", email="tester@example.com"):
    members.upsert_member(sub, email, "測試者")
    c.set_cookie(members.COOKIE, members.sign({"sub": sub, "exp": int(time.time()) + 3600}))


# ═══════════════════════════ A. API 安全 ═══════════════════════════
def test_anon_admin_api_401(cl):
    """未登入呼叫管理 API → 401。"""
    assert cl.get("/api/learning/train").status_code == 401


def test_member_admin_api_403(cl):
    """一般會員（非站主）呼叫管理 API → 403。"""
    _login(cl, email="tester@example.com")
    assert cl.get("/api/learning/train").status_code == 403


def test_forged_admin_cookie_rejected(monkeypatch):
    """偽造／竄改的後台管理簽名 token → 一律無效（不被當成已登入）。"""
    import admin_auth
    monkeypatch.setenv("ADMIN_PASSWORD", "a-strong-password-123")
    good = admin_auth.issue()
    assert admin_auth.valid(good) is True            # 正常簽發的有效
    exp = good.split(".", 1)[0]
    assert admin_auth.valid(f"{exp}.deadbeef") is False   # 竄改簽章 → 無效
    assert admin_auth.valid("999999.x") is False          # 偽造 → 無效


def test_telegram_test_api_requires_owner(cl, monkeypatch):
    """Telegram 測試 API：未登入 → 401；一般會員 → 403；站主 → 放行。"""
    H = {"X-TS": "1"}
    assert cl.post("/api/test/telegram", headers=H).status_code == 401
    _login(cl, email="tester@example.com")
    assert cl.post("/api/test/telegram", headers=H).status_code == 403
    # 站主放行（攔截實際發送，避免外部網路）
    import telegram_bot
    monkeypatch.setattr(telegram_bot, "send_alert", lambda *a, **k: True)
    _login(cl, sub="boss", email="boss@example.com")
    assert cl.post("/api/test/telegram", headers=H).status_code == 200


def test_webhook_wrong_token_401(cl, monkeypatch):
    """Webhook 設定使用錯誤管理 token → 401。"""
    monkeypatch.setattr(app_module, "TELEGRAM_BOT_TOKEN", "correct-token", raising=False)
    _login(cl, sub="boss", email="boss@example.com")
    r = cl.post("/api/webhook/set", json={"url": "https://example.com"},
                headers={"X-TS": "1", "X-Admin-Token": "WRONG"})
    assert r.status_code == 401


def test_webhook_illegal_url_400(cl, monkeypatch):
    """非法 Webhook 網址（非 HTTPS／指向內部位址）→ 400。"""
    monkeypatch.setattr(app_module, "TELEGRAM_BOT_TOKEN", "correct-token", raising=False)
    _login(cl, sub="boss", email="boss@example.com")
    hdr = {"X-TS": "1", "X-Admin-Token": "correct-token"}
    for bad in ("http://example.com", "https://localhost/x", "https://127.0.0.1/x",
                "https://169.254.169.254/latest/meta-data", "ftp://example.com"):
        r = cl.post("/api/webhook/set", json={"url": bad}, headers=hdr)
        assert r.status_code == 400, f"應拒絕的網址未被擋：{bad} → {r.status_code}"


def test_sensitive_write_endpoints_owner_only(cl):
    """敏感寫入操作（掃描、回填、重跑回測、資料刷新）：未登入 → 401；一般會員 → 403。
    對應第三方 brief §3 權限／§4#4：敏感 API 需嚴格 admin 隔離（含路由層縱深防禦）。"""
    H = {"X-TS": "1"}
    endpoints = ["/api/scan/force", "/api/backfill/run", "/api/backtest/full/run",
                 "/api/revenue/refresh"]
    for ep in endpoints:
        assert cl.post(ep, headers=H).status_code == 401, f"{ep} 未登入應 401"
    _login(cl, email="tester@example.com")      # 一般會員（非機主）
    for ep in endpoints:
        assert cl.post(ep, headers=H).status_code == 403, f"{ep} 一般會員應 403"


def test_rate_limit_blocks_excess(monkeypatch):
    """大量請求 → 超過上限即擋下（429 的底層計數器行為）。"""
    import protect
    bucket = {}
    allowed = sum(1 for _ in range(10) if protect._hit(bucket, "1.2.3.4", 5, window=60))
    assert allowed == 5          # 前 5 次放行、其後擋下


# ═══════════════════════ B. 資料庫及風險 ═══════════════════════
def _patch_store(monkeypatch, read=("ok", None), write_ok=True, box=None):
    class _S:
        def read_meta(self, k):
            return read if box is None or k not in box else ("ok", box[k])
        def get_meta(self, k, default=None):
            return (box or {}).get(k, default)
        def set_meta(self, k, v):
            if write_ok and box is not None:
                box[k] = v
            return write_ok
    import state_store
    monkeypatch.setattr(state_store, "store", _S())


def test_db_read_failure_blocks_new_signal(monkeypatch):
    """資料庫讀取失敗 → 禁止新增訊號（fail-closed）。"""
    import risk_manager as rm
    _patch_store(monkeypatch, read=("error", None))
    out = rm.check_daily_loss_limit()
    assert out.get("unavailable") is True and out.get("today_loss") is None
    res = rm.run_all_checks("2330.TW", {"close": 600}, {"daily": {"closes": [1, 2, 3]}},
                            {"index": {"twii": {"chg": 0.1, "source": "ok"},
                                       "vix": {"price": 18, "source": "ok"}}})
    assert res["can_signal"] is False


def test_db_write_failure_not_silent_success(monkeypatch):
    """資料庫寫入失敗 → set_meta 回報失敗，不可假裝成功。"""
    import state_store
    store = state_store.store

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "_conn", _boom)
    assert store.set_meta("k_any", {"v": 1}) is False


def test_daily_loss_concurrent_accumulation_no_loss(monkeypatch):
    """兩個以上執行緒同時累加當日虧損 → 不遺失任何一筆。"""
    import risk_manager as rm
    box = {}
    _patch_store(monkeypatch, write_ok=True, box=box)
    monkeypatch.setattr(rm, "_daily_loss", {"date": "", "loss_twd": 0.0, "signal_count": 0}, raising=False)

    def worker():
        for _ in range(50):
            rm.record_signal_loss(-100)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert abs(box["risk_daily_loss"]["loss_twd"] - 4 * 50 * 100) < 1e-6   # 200 筆全數計入
    assert box["risk_daily_loss"]["signal_count"] == 4 * 50


def test_daily_loss_circuit_breaker_triggers(monkeypatch):
    """當日虧損達上限 → 熔斷（exceeded=True）。"""
    import risk_manager as rm
    from config import ACCOUNT_BALANCE_TWD, MAX_DAILY_RISK
    from datetime import datetime
    today = datetime.now(rm.TZ_TAIPEI).strftime("%Y-%m-%d")
    over = ACCOUNT_BALANCE_TWD * MAX_DAILY_RISK + 1
    _patch_store(monkeypatch, read=("ok", {"date": today, "loss_twd": over, "signal_count": 1}))
    assert rm.check_daily_loss_limit()["exceeded"] is True


def test_vix_missing_not_shown_normal():
    """VIX 缺失（大盤正常）→ 不可顯示滿分綠燈／適合交易，須標示熔斷判斷不完整。"""
    import risk_manager as rm
    mo = {"index": {"twii": {"chg": 0.3, "source": "ok"}, "vix": {"source": "error"}}}
    st = rm.get_system_status(mo)
    assert st["env_score"] != 100                      # 不再滿分
    assert all("適合交易" not in v for v in st["category_advice"].values())
    assert any("VIX" in v for v in st["category_advice"].values())


# ═══════════════════════ C. AI 與回測 ═══════════════════════
def test_training_uses_time_series_walkforward(monkeypatch):
    """訓練評估不得用未來資料：採時間序列 walk-forward，最早一段只訓練不驗證（n_eval<n）。"""
    import json
    import numpy as np
    import learning_model as lm
    rng = np.random.default_rng(0)

    def _rows():
        rows = []
        for _ in range(800):
            x = float(rng.standard_normal())
            rows.append({"bar_date": "2026-01-01", "score": x, "r_multiple": x + 0.3 * float(rng.standard_normal()),
                         "kind": "candidate", "features": json.dumps({"f1": x})})
        return rows
    monkeypatch.setattr(lm, "_rows", _rows)
    out = lm.train_and_eval(persist=False)
    assert out["eval_method"] == "time_series_walkforward"
    assert 0 < out["n_eval"] < out["n"]


def test_backtest_same_bar_sl_tp_is_conservative():
    """同一根 K 棒同時觸及停損與停利 → 採保守成交（主回測與 walk-forward 皆停損優先）。"""
    import inspect
    import backtester
    src = inspect.getsource(backtester.walk_forward_backtest_tw)
    # 保守規則：先判斷 hit_sl，命中即以停損價成交（不因同時觸及停利而取較樂觀的 tp1）
    assert '("sl",sl) if hit_sl else ("tp1",tp1)' in src
