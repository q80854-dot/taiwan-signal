"""Champion 策略版本與凍結（2026-11-08，治理 TS-GOV-001/003）。

目的：把「決定線上 Champion 決策行為」的策略/風控常數集中成一份可雜湊的快照，產生穩定的
config_hash 與版本字串，用來：
  1) 讓每筆訊號可回溯到產生它的策略版本（provenance）。
  2) 以回歸測試（test_champion_freeze）把 Champion 鎖住——任何人改到被追蹤的常數，測試就會紅，
     逼出一個有意識的 PR＋使用者核准才能更新快照（變更控管）。

本模組只讀 config、計算雜湊，不改任何策略值、不影響決策。被追蹤的是「策略邏輯常數」，
刻意排除：金鑰、使用者資金（ACCOUNT_BALANCE_TWD，屬個人配置非策略）、時區/假日/自選清單/
系統版本字串等非決策項。env 可覆寫的項目（MAX_PORTFOLIO_RISK 等）以「當前生效值」入雜湊——
CI 無 env 覆寫時＝程式預設，故凍結測試鎖的是程式碼預設；線上實際生效值由端點查詢。
"""
import hashlib
import json

import config as _cfg

# 被追蹤的策略/風控常數（改這些＝改 Champion 行為，須經你核准並更新快照）
_TRACKED = [
    "MAX_RISK_PER_TRADE", "MAX_DAILY_RISK", "MAX_SIMULTANEOUS_POSITIONS", "MAX_PORTFOLIO_RISK",
    "MAX_PER_CORRELATION_GROUP",
    "EVENT_MATERIAL_NEWS_PENALTY", "EVENT_EXDIV_BLOCK_DAYS", "YIELD_ANOMALY_PCT",
    "COMMISSION_RATE", "TAX_RATE_SELL", "SHARES_PER_LOT", "MIN_COMMISSION",
    "SLIPPAGE_RATE", "SHORT_BORROW_ANNUAL",
    "INDICATOR_PARAMS", "TIMEFRAMES", "SIGNAL_THRESHOLDS",
    "ENABLE_SHORT_SIGNALS", "SHORT_EXECUTABLE", "SHORT_SIGNAL_THRESH",
    "LONG_SIGNAL_THRESH", "LONG_REQUIRE_WEEKLY_BULLISH",
    "SHORT_MAX_SENTIMENT", "SHORT_REQUIRE_WEEKLY_BEARISH",
    "SWING_PARAMS", "CIRCUIT_BREAKER", "CORRELATION_GROUPS",
    "EARNINGS_SEASON_DEADLINES", "EARNINGS_SEASON_LOOKBACK_DAYS",
]


def _canon(v):
    """遞迴轉成 JSON 可序列化、順序穩定的結構：set→排序 list、tuple→list、dict→排序鍵。"""
    if isinstance(v, dict):
        return {str(k): _canon(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, set):
        return sorted(_canon(x) for x in v)
    return v


def config_snapshot() -> dict:
    """回傳被追蹤常數的 {名稱: 正規化值}。缺少的常數標 "__MISSING__"（代表 config 結構變了）。"""
    out = {}
    for name in _TRACKED:
        out[name] = _canon(getattr(_cfg, name, "__MISSING__"))
    return out


def config_hash() -> str:
    """被追蹤常數的 SHA-256（canonical JSON，鍵排序）。常數不變則雜湊穩定、跨機一致。"""
    blob = json.dumps(config_snapshot(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def strategy_version() -> str:
    return "champion-" + config_hash()[:12]


def version_info() -> dict:
    """供端點/報告顯示：版本、完整雜湊、被追蹤常數清單與當前生效值。"""
    return {"strategy_version": strategy_version(), "config_hash": config_hash(),
            "tracked": _TRACKED, "constants": config_snapshot()}
