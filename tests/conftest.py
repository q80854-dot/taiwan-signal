import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 測試一律用暫存的 SQLite，不碰正式資料庫
os.environ.pop("DATABASE_URL", None)
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
