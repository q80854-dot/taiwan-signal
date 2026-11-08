#!/usr/bin/env python3
"""重新產生 Champion 凍結快照（治理 TS-GOV-003，變更控管用）。

什麼時候跑：你『有意識地』改了被追蹤的策略/風控常數（config.py，見
strategy_version._TRACKED）、而且這是你核准的 Champion 行為變更之後。跑完會把
docs/ai-governance/CHAMPION_SNAPSHOT.json 更新成新的常數與 config_hash，
test_champion_freeze 才會再次轉綠。

用法：
    python scripts/freeze_champion.py            # 寫入快照（會先顯示將變更的雜湊）
    python scripts/freeze_champion.py --check    # 只比對，不寫入（給人看目前差異）

本腳本只讀 config、寫 docs 下的快照檔，不碰任何正式策略值、不影響決策。
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import strategy_version as sv  # noqa: E402

SNAP = os.path.join(ROOT, "docs", "ai-governance", "CHAMPION_SNAPSHOT.json")
NOTE = ("此為 Champion 正式策略/風控常數的凍結快照。要更新，須有意識地改 config 常數並經"
        "使用者核准後重新產生本檔（python scripts/freeze_champion.py）。")


def _load_existing() -> dict:
    try:
        with open(SNAP, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def build() -> dict:
    info = sv.version_info()
    return {
        "config_hash": info["config_hash"],
        "strategy_version": info["strategy_version"],
        "frozen_at": datetime.date.today().isoformat(),
        "note": NOTE,
        "constants": info["constants"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比對目前 config_hash 與快照，不寫入")
    args = ap.parse_args()

    existing = _load_existing()
    cur_hash = sv.config_hash()
    old_hash = existing.get("config_hash", "(無)")

    if args.check:
        same = cur_hash == existing.get("config_hash")
        print(f"快照 config_hash : {old_hash}")
        print(f"目前 config_hash : {cur_hash}")
        print("一致 ✅" if same else "不一致 ❌（Champion 常數已變更，需核准後重新產生快照）")
        return 0 if same else 1

    snap = build()
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"已寫入 {os.path.relpath(SNAP, ROOT)}")
    print(f"  舊 config_hash: {old_hash}")
    print(f"  新 config_hash: {snap['config_hash']}")
    print(f"  strategy_version: {snap['strategy_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
