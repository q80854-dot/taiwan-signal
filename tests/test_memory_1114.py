"""2026-11-14 記憶體稽核修正（使用者同意）的回歸測試：快取有上限、查詢只讀需要的資料、過期持久快取可清。"""
import time
import uuid
from datetime import datetime, timedelta, timezone


def test_inst_since_filters_by_code_in_sql():
    from state_store import store
    rows = store.get_inst_since("2000-01-01", code="__nonexistent__")
    assert rows == []
    import inspect
    assert "code=?" in inspect.getsource(store.get_inst_since)


def test_protect_cache_has_byte_cap_and_purges_expired(monkeypatch):
    import protect
    assert protect.CACHE_MAX_BYTES <= 64 * 1024 * 1024
    src = open(protect.__file__, encoding="utf-8").read()
    assert "CACHE_MAX_BYTES" in src and "v[0] <= _now" in src


def test_finmind_cache_bounded():
    import finmind as FM
    FM._cache.clear()
    for i in range(FM._CACHE_MAX + 50):
        FM._put(f"k{i}", {"at": i, "data": None})
    assert len(FM._cache) <= FM._CACHE_MAX
    assert "k0" not in FM._cache and f"k{FM._CACHE_MAX + 49}" in FM._cache     # 最舊的先被移除
    FM._cache.clear()


def test_finmind_data_cache_cap_lowered():
    import finmind_data
    assert finmind_data._MAX_CACHE <= 300


def test_get_stock_info_any_uses_index(monkeypatch):
    import stock_universe as su
    calls = []
    uni = [{"ticker": "2330.TW", "code": "2330"}, {"ticker": "6488.TWO", "code": "6488"}]
    monkeypatch.setattr(su, "build_full_universe", lambda *a, **k: calls.append(1) or uni)
    monkeypatch.setattr(su, "_ANY_IDX", None)
    assert su.get_stock_info_any("2330.TW")["code"] == "2330"
    assert su.get_stock_info_any("6488.TWO")["code"] == "6488"
    assert su.get_stock_info_any("9999.TW") is None
    assert len(calls) == 1                                  # 60 秒內共用同一份索引


def test_prune_meta_prefix_only_old_matching_keys():
    from state_store import store
    tag = uuid.uuid4().hex[:8]
    old_k, new_k, other_k = f"fmtest{tag}:old", f"fmtest{tag}:new", f"keep{tag}:old"
    for k in (old_k, new_k, other_k):
        store.set_meta(k, "x")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with store._conn() as conn:
        conn.execute("UPDATE meta SET updated_at=? WHERE key IN (?,?)", (old_ts, old_k, other_k))
    n = store.prune_meta_prefix(f"fmtest{tag}:", 40)
    assert n == 1
    assert store.get_meta(old_k, None) is None
    assert store.get_meta(new_k, None) == "x" and store.get_meta(other_k, None) == "x"
    assert store.prune_meta_prefix("", 40) == 0 and store.prune_meta_prefix("fm:", 1) == 0   # 防呆
