"""策略引擎 Phase 1 骨架（§三 3.1，2026-11-11）。

驗證共用介面契約、註冊表、shadow strategy_name 欄位 migration。純 shadow，不接線上。
"""
import pytest
from strategies import base, registry


# ───────── 介面契約 ─────────
_PLAN = {"direction": "long", "entry": 100.0, "stop": 95.0,
         "tp1": 103.0, "tp2": 106.0, "tp3": 110.0, "score": 80, "reason": "x"}


def test_validate_plan_ok():
    assert base.validate_plan(dict(_PLAN)) is True


def test_validate_plan_missing_key():
    bad = dict(_PLAN); bad.pop("stop")
    assert base.validate_plan(bad) is False


def test_validate_plan_bad_direction():
    assert base.validate_plan(dict(_PLAN, direction="up")) is False


def test_validate_plan_non_numeric_price():
    assert base.validate_plan(dict(_PLAN, entry="100")) is False
    assert base.validate_plan(dict(_PLAN, score=True)) is False   # bool 不算數值


def test_base_generate_not_implemented():
    with pytest.raises(NotImplementedError):
        base.Strategy().generate("2330.TW", {}, {}, {})


def test_regime_applicability_default():
    assert base.Strategy().regime_applicability({}) == 1.0


# ───────── 註冊表 ─────────
class _Dummy(base.Strategy):
    name = "dummy_test"
    direction = "long"

    def generate(self, *a, **k):
        return None


def test_registry_register_and_lookup():
    registry.clear()
    s = _Dummy()
    assert registry.register(s) is s
    assert registry.get("dummy_test") is s
    assert registry.names() == ["dummy_test"]
    assert registry.all_strategies() == [s]
    registry.clear()
    assert registry.names() == []


def test_registry_requires_name():
    registry.clear()
    bad = base.Strategy(); bad.name = ""
    with pytest.raises(ValueError):
        registry.register(bad)


def test_registry_sorted_order():
    registry.clear()
    a = _Dummy(); a.name = "aaa"
    z = _Dummy(); z.name = "zzz"
    registry.register(z); registry.register(a)
    assert registry.names() == ["aaa", "zzz"]      # 穩定排序
    registry.clear()


# ───────── shadow 欄位 migration ─────────
def test_shadow_has_strategy_name_column():
    import shadow
    from state_store import store
    shadow.ensure_table()
    with store._conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(shadow_signals)").fetchall()]
    assert "strategy_name" in cols
