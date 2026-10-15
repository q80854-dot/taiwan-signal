import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 測試一律用暫存的 SQLite，不碰正式資料庫
os.environ.pop("DATABASE_URL", None)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")


import pytest


@pytest.fixture(autouse=True)
def _reset_protect():
    """每個測試前清掉防護層的快取與限流計數，避免互相污染。"""
    try:
        import protect
        protect.clear_cache(); protect._rate.clear(); protect._heavy_rate.clear(); protect._denies.clear(); protect._banned.clear()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _hermetic_network_and_caches(monkeypatch):
    """讓測試完全不打真實外網，並清掉會跨測試污染的模組快取。

    ★ 2026-10-15：CI 曾出現 test_company_info 間歇性失敗（本機永遠通過）。根因是部分測試會打
    /api/instruments，而個股端點改成並行抓取後，在 CI（有部分網路、逾時很慢）會留下仍卡在真實
    HTTP 的背景執行緒，稍後回來把共用的 company_info._cache 覆寫成真實/None 值，污染後面的
    company_info 測試。本機因為完全沒有外網，抓取瞬間失敗、不會有殘留執行緒，所以不會重現。
    這裡讓真實外網請求『瞬間失敗』（等同本機行為、也讓 CI 更快），並在每個測試前後清掉
    company_info 的模組快取，從根本消除這類跨測試污染。各測試自己 monkeypatch 的
    `模組.requests.get=fake` 是直接替換函式、不經過這層，不受影響。"""
    try:
        import requests.adapters
        def _blocked_send(self, *a, **k):
            raise requests.exceptions.ConnectionError("outbound network disabled in tests")
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked_send, raising=False)
    except Exception:
        pass
    try:
        import company_info
        company_info._cache.clear()
    except Exception:
        company_info = None
    yield
    try:
        if company_info is not None:
            company_info._cache.clear()
    except Exception:
        pass
