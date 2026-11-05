"""來源連線健檢：可選 AI 報告來源（Gemini/Groq/Anthropic）連線異常不得升級為 fail 紅燈
（2026-11-05）。關鍵資料來源仍須 fail。對齊『減少噪音、非關鍵功能優雅降級』原則。"""
import source_health as sh


def _reset():
    with sh._LOCK:
        sh._hosts.clear()


def _down(host, n=3):
    url = f"https://{host}/x"
    for _ in range(n):
        sh.record(url, ok=False, ms=100.0, code=None, err="timeout")


def _ok(host, n=3):
    url = f"https://{host}/x"
    for _ in range(n):
        sh.record(url, ok=True, ms=50.0, code=200)


def test_optional_llm_down_is_warn_not_fail():
    _reset()
    _ok("openapi.twse.com.tw")
    _down("generativelanguage.googleapis.com")
    lvl, msg = sh.summary()
    assert lvl == "warn", f"可選 AI 來源連線異常應為 warn，不得 fail：{msg}"
    assert "退回規則式" in msg
    assert "Gemini" in msg


def test_critical_down_still_fail():
    _reset()
    _ok("api.groq.com")        # 可選來源正常
    _down("openapi.twse.com.tw")
    lvl, msg = sh.summary()
    assert lvl == "fail"
    assert "證交所" in msg


def test_critical_and_optional_both_down_fail_mentions_optional():
    _reset()
    _down("www.twse.com.tw")
    _down("generativelanguage.googleapis.com")
    lvl, msg = sh.summary()
    assert lvl == "fail"                     # 有關鍵來源異常 → 仍 fail
    assert "非關鍵 AI 報告來源" in msg        # 但附帶說明可選來源也異常
    assert "1 個來源連線異常" in msg          # 關鍵只算 1 個（TWSE），不含可選


def test_all_ok():
    _reset()
    _ok("openapi.twse.com.tw")
    _ok("api.anthropic.com")
    lvl, msg = sh.summary()
    assert lvl == "ok"
