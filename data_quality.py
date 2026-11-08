"""資料品質狀態（治理 Phase 2 / PIT 起步 A，2026-11-08）。

把「這筆資料目前能不能信」標準化成一組列舉，取代散落各處的 data_available 布林。
本模組只做分類（純函式、無副作用、不打網路、不碰決策），供 risk_manager.get_system_status
等顯示/風控回報使用；是否因品質不佳而擋單，仍由既有 fail-closed 邏輯決定，本模組不介入。

狀態語意（對齊雙 AI 交接文件 §6）：
  VALID    資料齊全、在可接受時效內。
  DELAYED  有資料但比預期晚（仍可用、需標示）。
  PARTIAL  只有部分必要欄位（例如大盤有、VIX 缺）。
  STALE    資料過期（超過允許時效）。
  MISSING  關鍵資料缺（抓不到）。
  CONFLICT 多來源互相矛盾。
  INVALID  資料存在但不可信（讀取失敗/格式錯/風控狀態未知）。
"""
from typing import Optional

VALID = "VALID"
DELAYED = "DELAYED"
PARTIAL = "PARTIAL"
STALE = "STALE"
MISSING = "MISSING"
CONFLICT = "CONFLICT"
INVALID = "INVALID"

# 由「最好」到「最差」的嚴重度排序；worst() 取最差者。
_SEVERITY = {VALID: 0, DELAYED: 1, PARTIAL: 2, STALE: 3, MISSING: 4, CONFLICT: 5, INVALID: 6}

# 哪些狀態代表「不應據以放行新單」（供顯示/風控參考；本模組不直接擋單）。
_NOT_TRADEABLE = {STALE, MISSING, CONFLICT, INVALID}


def worst(*statuses: Optional[str]) -> str:
    """回傳一組狀態中最嚴重者；空輸入或全 None 視為 VALID。"""
    present = [s for s in statuses if s]
    if not present:
        return VALID
    return max(present, key=lambda s: _SEVERITY.get(s, _SEVERITY[INVALID]))


def is_tradeable(status: str) -> bool:
    """此品質狀態是否足以放行新單（VALID/DELAYED/PARTIAL 可、其餘不可）。"""
    return status not in _NOT_TRADEABLE


def classify_freshness(available_time: Optional[float], now: float,
                       max_age_seconds: Optional[float]) -> str:
    """以「資料可取得時間」與「現在」的差距判定時效（PIT 的核心原語）。
    available_time 為 None → MISSING；未設時效上限 → VALID；超過上限 → STALE。
    （負差距＝available_time 在未來，視為 INVALID，代表時間戳有誤。）"""
    if available_time is None:
        return MISSING
    age = now - available_time
    if age < 0:
        return INVALID
    if max_age_seconds is None:
        return VALID
    return STALE if age > max_age_seconds else VALID
