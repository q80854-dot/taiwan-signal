# gunicorn 會自動讀取專案根目錄的 gunicorn.conf.py（不必改 Render 的啟動指令）。
# 原本是 1 個同步 worker：一個慢請求（例如抓資料）就會讓其他人排隊等待。
# 改成 1 個 worker + 4 個執行緒：排程器只會啟動一份，同時卻能處理多個請求。
worker_class = "gthread"
workers = 1
threads = 12
timeout = 120
graceful_timeout = 30
keepalive = 5
backlog = 512          # 瞬間湧入時，先排隊而不是直接拒絕連線
max_requests = 0
