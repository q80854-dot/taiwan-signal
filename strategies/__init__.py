"""策略引擎套件（§三 3.1，Phase 1 骨架，2026-11-11）。

純 shadow 研究用：定義多個獨立策略的共用介面與註冊表。**本套件不接掃描決策、不影響
實際發出的線上訊號、不碰 Champion、不改 config_hash**。策略要接入 regime 路由（線上）
需足量 OOS 證據＋使用者明確核准（risk:champion），屬後續階段。
"""


def load_builtin():
    """載入並註冊所有內建策略。掃描端（階段 2b 起）呼叫一次後用 registry.all_strategies()
    取得清單。明確建構＋註冊（不依賴 import 副作用，呼叫多次結果一致）。"""
    from . import registry
    from .trend_following import TrendFollowing
    registry.register(TrendFollowing())

