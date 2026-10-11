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


def test_localhost_ignored():
    _reset()
    sh.record("http://127.0.0.1:5000/api/x", True, 1.0, 200)
    assert sh.snapshot()["sources"] == []
