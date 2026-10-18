"""法人單位顯示校正（2026-10-18）。

交易紀錄顯示「外資買超 +52,829,071張」：數字是「股」卻標成「張」。根因是 T86 股→張
換算（÷1000）2026-10-10 才上線，之前存進資料庫的 inst_signal 字串是舊格式；這些歷史
字串無法重新推導，只能在顯示當下做非破壞性校正。這裡驗證：
  - 產生時間早於修正日（2026-10-10）＝舊格式 → 一律 ÷1000 還原成張（大小不限）。
  - |數字| ≥ 30 萬張＝單一個股物理上不可能 → 視為「股」被標成張，校正（涵蓋邊界漏網）。
  - 修正日之後、且數值在合理張量級的正確資料 → 完全不動（不誤傷）。
  - 沒有「張」字、或空字串 → 原樣回傳。
"""
from state_store import normalize_legacy_inst_signal


def test_legacy_by_date_divides_by_1000():
    s = {"inst_signal": "外資買超 +52,829,071張", "generated_at": "2026-10-03T13:30:00+00:00"}
    out = normalize_legacy_inst_signal(s)
    assert out["inst_signal"] == "外資買超 +52,829張"


def test_legacy_sell_keeps_sign_and_warning_suffix():
    s = {"inst_signal": "外資賣超 -1,234,567張 ⚠️", "generated_at": "2026-09-30T00:00:00+00:00"}
    out = normalize_legacy_inst_signal(s)
    assert out["inst_signal"] == "外資賣超 -1,235張 ⚠️"


def test_impossible_magnitude_fixed_even_if_recent():
    # 修正日之後，但數字落在「股」量級（物理上不可能的張）→ 仍校正（邊界安全網）
    s = {"inst_signal": "外資買超 +9,900,000張", "generated_at": "2026-10-17T13:30:00+00:00"}
    out = normalize_legacy_inst_signal(s)
    assert out["inst_signal"] == "外資買超 +9,900張"


def test_recent_plausible_value_untouched():
    # 修正日之後、合理張量級的正確資料：原樣不動
    s = {"inst_signal": "外資買超 +52,829張", "generated_at": "2026-10-17T13:30:00+00:00"}
    out = normalize_legacy_inst_signal(s)
    assert out["inst_signal"] == "外資買超 +52,829張"


def test_no_lots_or_empty_passthrough():
    assert normalize_legacy_inst_signal({"inst_signal": "", "generated_at": "2026-09-01"})["inst_signal"] == ""
    assert normalize_legacy_inst_signal({"generated_at": "2026-09-01"}).get("inst_signal") is None
    # 沒有「張」就不處理
    s = {"inst_signal": "外資連續買超 5 天", "generated_at": "2026-09-01"}
    assert normalize_legacy_inst_signal(s)["inst_signal"] == "外資連續買超 5 天"
