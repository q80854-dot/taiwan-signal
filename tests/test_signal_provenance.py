"""訊號可回溯性（治理 TS-GOV-001，2026-11-08）。

每筆訊號落盤時都要蓋上「產生它的 Champion 策略版本」——把 strategy_version 與
config_hash 寫進 raw_json。這樣任何一筆歷史訊號日後都能回答「這是哪一版策略/風控
常數產生的」，是變更控管與事後歸因的基礎。純 metadata，不改任何決策值。
"""
import json

import strategy_version as sv
from state_store import store


def _minimal_sig(sig_id: str) -> dict:
    return {
        "id": sig_id, "ticker": "2330.TW", "name": "台積電", "direction": "buy",
        "score": 7.5, "current_price": 1000.0, "entry_price": 1000.0,
        "stop_loss": 950.0, "tp1": 1080.0,
    }


def _raw_json_of(sig_id: str) -> dict:
    with store._conn() as conn:
        row = conn.execute("SELECT raw_json FROM signals WHERE id=?", (sig_id,)).fetchone()
    assert row is not None, "訊號未寫入資料庫"
    return json.loads(row["raw_json"])


def test_saved_signal_carries_champion_config_hash():
    """落盤後的 raw_json 必帶 config_hash／strategy_version，且與當前 Champion 一致。"""
    sid = "prov-test-001"
    assert store.save_signal(_minimal_sig(sid)) is True
    raw = _raw_json_of(sid)
    assert raw["config_hash"] == sv.config_hash()
    assert raw["strategy_version"] == sv.strategy_version()


def test_provenance_does_not_clobber_signal_fields():
    """provenance 只是附加 metadata，不得覆蓋原訊號的任何既有欄位。"""
    sid = "prov-test-002"
    sig = _minimal_sig(sid)
    assert store.save_signal(sig) is True
    raw = _raw_json_of(sid)
    assert raw["ticker"] == "2330.TW"
    assert raw["score"] == 7.5
    assert raw["direction"] == "buy"
