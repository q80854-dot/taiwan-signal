"""資料來源連線檢測（2026-10-11）：依主機彙總最近 N 次請求的成功率、延遲、狀態。"""
import source_health as sh


def _reset():
    sh._hosts.clear()


def test_records_and_rates():
    _reset()
    for _ in range(9):
        sh.record("https://openapi.twse.com.tw/v1/x", True, 120.0, 200)
    sh.record("https://openapi.twse.com.tw/v1/x", False, 5000.0, None, "timeout")
    snap = sh.snapshot()
    row = [r for r in snap["sources"] if r["host"] == "openapi.twse.com.tw"][0]
    assert row["attempts"] == 10
    assert row["success_rate"] == 90          # 9/10
    assert row["p50_ms"] is not None
    # 最後一次是失敗 → 至少 warn，且備註帶出錯誤
    assert row["state"] in ("warn", "down")
    assert row["last_err"]


def test_all_fail_is_down():
    _reset()
    for _ in range(4):
        sh.record("https://api.coingecko.com/x", False, 3000.0, None, "proxy 403")
    snap = sh.snapshot()
    row = snap["sources"][0]
    assert row["state"] == "down"
    assert row["success_rate"] == 0
    lvl, msg = sh.summary()
    assert lvl == "fail" and "連線異常" in msg


def test_all_ok_is_ok():
    _reset()
    for _ in range(5):
        sh.record("https://www.taifex.com.tw/x", True, 80.0, 200)
    snap = sh.snapshot()
    assert snap["sources"][0]["state"] == "ok"
    assert sh.summary()[0] == "ok"


def test_quota_limited_is_not_down():
    # FinMind 額度達上限（HTTP 402）：連線其實正常，應標 limited、健檢留意(warn)，不是異常(fail)
    _reset()
    for _ in range(5):
        sh.record("https://api.finmindtrade.com/api/v4/data", False, 300.0, 402, None)
    snap = sh.snapshot()
    row = snap["sources"][0]
    assert row["state"] == "limited"
    assert row["success_rate"] == 100           # 連得上（額度問題不算連線失敗）
    lvl, msg = sh.summary()
    assert lvl == "warn" and "額度" in msg and "連線異常" not in msg


def test_recovers_from_limited_to_ok():
    _reset()
    for _ in range(4):
        sh.record("https://api.finmindtrade.com/x", False, 200.0, 429, None)
    assert sh.snapshot()["sources"][0]["state"] == "limited"
    for _ in range(3):
        sh.record("https://api.finmindtrade.com/x", True, 150.0, 200)
    assert sh.snapshot()["sources"][0]["state"] == "ok"


def test_real_failure_still_down_even_with_past_quota():
    # 先額度上限，之後變成真正連不上（逾時）→ 要能回到 down，不被 limited 掩蓋
    _reset()
    sh.record("https://api.finmindtrade.com/x", False, 200.0, 402, None)
    for _ in range(3):
        sh.record("https://api.finmindtrade.com/x", False, 5000.0, None, "timeout")
    assert sh.snapshot()["sources"][0]["state"] == "down"


def test_localhost_ignored():
    _reset()
    sh.record("http://127.0.0.1:5000/api/x", True, 1.0, 200)
    assert sh.snapshot()["sources"] == []
