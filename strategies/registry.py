"""策略註冊表（Phase 1 骨架）。純 shadow，不接掃描決策。

策略模組載入時以 register() 登錄；掃描端（Phase 2 起）用 all_strategies() 取得清單、平行記錄
各策略的假想訊號到 shadow（kind='strategy_candidate'），不發 Telegram、不進風控、不影響線上訊號。
"""
_REGISTRY = {}


def register(strategy):
    """登錄一個策略實例（以 strategy.name 為鍵；同名覆寫）。回傳該實例，便於裝飾器式使用。"""
    name = getattr(strategy, "name", None)
    if not name or not isinstance(name, str):
        raise ValueError("strategy 必須有非空字串 name")
    _REGISTRY[name] = strategy
    return strategy


def get(name):
    return _REGISTRY.get(name)


def all_strategies():
    """回傳所有已登錄策略（依 name 排序，順序穩定）。"""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def names():
    return sorted(_REGISTRY)


def clear():
    """清空註冊表（測試用）。"""
    _REGISTRY.clear()
