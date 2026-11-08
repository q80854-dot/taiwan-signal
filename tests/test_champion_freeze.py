"""Champion 凍結與變更控管（治理 TS-GOV-001/003，2026-11-08）。

這個測試是「正式策略的鎖」：若有人改到被追蹤的策略/風控常數（config.py），
config_hash 會變、與 docs/ai-governance/CHAMPION_SNAPSHOT.json 不符，測試即紅——
逼出一個有意識的 PR，提醒這是改 Champion 行為，須經使用者核准並重新產生快照後才放行。
純治理檢查，不改任何策略值。
"""
import json
import os

import strategy_version as sv

_SNAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "docs", "ai-governance", "CHAMPION_SNAPSHOT.json")


def _snapshot():
    with open(_SNAP, encoding="utf-8") as f:
        return json.load(f)


def test_config_hash_is_stable():
    """同一份 config 連算兩次雜湊必相同（可重現、跨機一致）。"""
    assert sv.config_hash() == sv.config_hash()


def test_champion_matches_frozen_snapshot():
    """現行策略/風控常數必須與凍結快照一致。不一致＝有人改了 Champion 行為。
    若這是你核准的變更：改完 config 後重新產生 CHAMPION_SNAPSHOT.json（見其 note），再提交。"""
    snap = _snapshot()
    assert sv.config_hash() == snap["config_hash"], (
        "Champion 策略/風控常數與凍結快照不符——這會改變線上決策行為。"
        "若為核准變更，請重新產生 docs/ai-governance/CHAMPION_SNAPSHOT.json；否則請還原 config。")
    assert sv.strategy_version() == snap["strategy_version"]


def test_snapshot_covers_all_tracked_constants():
    """快照必須涵蓋所有被追蹤常數，且無 __MISSING__（代表 config 結構被改動）。"""
    snap = _snapshot()
    for name in sv._TRACKED:
        assert name in snap["constants"], f"快照缺少被追蹤常數：{name}"
        assert snap["constants"][name] != "__MISSING__", f"config 缺少常數：{name}"
