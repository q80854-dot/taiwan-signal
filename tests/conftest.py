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
        protect.clear_cache(); protect._rate.clear(); protect._heavy_rate.clear()
    except Exception:
        pass
    yield
