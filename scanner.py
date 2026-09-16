"""
scanner.py — 全市場掃描引擎 v1.0
每日 16:30 盤後觸發，批次掃描 1000+ 檔台股
"""
import time, logging, threading, concurrent.futures, gc
from datetime import datetime, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

from config import SIGNAL_THRESHOLDS as THRESH, CIRCUIT_BREAKER as CB, SYSTEM, TELEGRAM_CONFIG

# ★ 修正：2026-09-03——重大 bug：_check_market_status() 之前會直接
# `THRESH["min_score"] = min(75, THRESH["min_score"] + 5)`，但 THRESH 就是
# config.py 裡 SIGNAL_THRESHOLDS 這個「同一個」dict 物件，被 scanner.py /
# signal_engine.py / backtester.py 三個檔案共用匯入。gunicorn worker 是長駐
# process、不會每天重啟，代表這個+5是「永久疊加、永不歸零」的：只要哪天
# 大盤偏弱觸發過一次 caution，門檻就從 65 變 70，下次又是 caution 天，
# 又變 75（觸頂），從此之後即使大盤恢復正常，門檻也永遠卡在 75，比原本
# 設計高 10 分，等於長期悄悄篩掉更多本來合格的訊號，且完全沒有任何 log
# 或機制會把它調回來。修正方式：每次開始新的一次掃描時，先把 min_score
# 重置回這個原始基準值，當次掃描如果偵測到大盤偏弱才臨時往上調，下一次
# 掃描又會重新從基準值開始，不會跨天累積。
_BASE_MIN_SCORE = THRESH["min_score"]


class TWScanEngine:

    def __init__(self):
        self.signals_today: List[Dict] = []
        self.scan_count:    int        = 0
        self.last_scan_at:  str        = ""
        self.scan_errors:   List[str]  = []
        # ★ 修正：2026-09-03——原本 run_daily_scan() 沒有任何「正在掃描中」的鎖，
        # 如果排程的每日掃描（16:30）跟手動觸發的 /api/scan/force（或使用者連點
        # 兩次網站上的「立即掃描」按鈕）同時執行，兩個掃描會同時打相同的資料來源
        # API、同時寫入 self.signals_today / SQLite，訊號可能重複推播兩次到
        # Telegram，且更容易把上游 API 打到限流。這裡加一個簡單的執行鎖，掃描
        # 進行中時直接跳過，不會排隊等待（避免使用者連點時卡住一堆執行緒）。
        self._scan_lock:    threading.Lock = threading.Lock()

    @property
    def is_scanning(self) -> bool:
        return self._scan_lock.locked()

    def run_daily_scan(self) -> Optional[Dict]:
        if not self._scan_lock.acquire(blocking=False):
            logger.warning("run_daily_scan: 已有掃描正在進行中，本次觸發跳過")
            return None
        try:
            return self._run_daily_scan_impl()
        finally:
            self._scan_lock.release()

    def _run_daily_scan_impl(self) -> Dict:
        start_time = time.time()
        logger.info("═══ 開始每日全市場掃描 ═══")

        from stock_universe import get_scan_batches, get_stock_info
        from data_fetcher   import fetch_all_timeframes, fetch_market_overview, fetch_stock_institutional, _cache_clear_all
        from signal_engine  import generate_signal_tw
        from state_store    import store

        # ★ 修正：2026-09-03——每次全市場掃描開始前先清空 data_fetcher 的記憶體內快取，
        # 避免同一小時內重複手動觸發掃描時，快取跨多次掃描疊加造成記憶體壓力
        # （詳見 data_fetcher._cache_clear_all 註解，這是 Render 512MB 方案上
        # instance 在掃描中途被平台自動重啟的主要懷疑原因）。
        try:
            _cache_clear_all()
        except Exception as e:
            logger.warning(f"_cache_clear_all: {e}")

        # 0. 結算舊訊號（見 _resolve_pending_signals 說明）
        logger.info("Step 0/5: 結算舊訊號的停損/停利...")
        try:
            self._resolve_pending_signals()
        except Exception as e:
            logger.error(f"_resolve_pending_signals: {e}", exc_info=True)

        # 1. 市場總覽
        logger.info("Step 1/5: 抓取市場環境...")
        try:
            market_overview = self._call_with_timeout(self._MARKET_OVERVIEW_TIMEOUT_SEC, fetch_market_overview)
        except concurrent.futures.TimeoutError:
            logger.error(f"fetch_market_overview 逾時（{self._MARKET_OVERVIEW_TIMEOUT_SEC}秒），本次改用空資料繼續掃描")
            market_overview = {}
        self._check_market_status(market_overview)

        # ★ 新增：2026-09-16——risk_manager.check_daily_loss_limit() / check_max_positions()
        # 這兩個熔斷檢查先前雖然定義完整，但稽核發現整個專案裡「從來沒有任何地方呼叫」，
        # 只在 /api/state 給網站顯示一個數字，今日虧損真的超過帳戶 6% 上限、或活躍持倉數
        # 真的達到上限時，系統仍然會繼續產生並推播新訊號——熔斷保護形同虛設。這裡在批次
        # 掃描開始前實際檢查一次：任一熔斷觸發就跳過本次 Step 2/3（不產生新訊號），但
        # Step 0 的舊訊號結算、Step 5 的（空）推播仍正常執行，並推播一則警示讓機主知道
        # 今天為什麼沒有新訊號，而不是誤以為系統掛了。
        skip_new_signals = False
        try:
            from risk_manager import check_daily_loss_limit, check_max_positions
            active_signals = store.get_pending_signals()
            daily_loss = check_daily_loss_limit()
            max_pos    = check_max_positions(active_signals)
            block_msgs = []
            if daily_loss.get("exceeded"):
                skip_new_signals = True
                block_msgs.append(daily_loss.get("message", "今日虧損達上限"))
            if max_pos.get("exceeded"):
                skip_new_signals = True
                block_msgs.append(max_pos.get("message", "持倉數達上限"))
            if skip_new_signals:
                logger.warning(f"風控熔斷觸發，本次掃描跳過產生新訊號：{'；'.join(block_msgs)}")
                try:
                    from telegram_bot import send_alert
                    send_alert("🛑 風控熔斷，今日暫停產生新訊號\n" + "\n".join(block_msgs), "warning")
                except Exception as e:
                    logger.warning(f"風控熔斷通知推播失敗: {e}")
        except Exception as e:
            logger.warning(f"risk_manager 熔斷檢查失敗（不影響本次掃描繼續）: {e}", exc_info=True)

        # 2. 品種清單
        logger.info("Step 2/5: 建立掃描清單...")
        if skip_new_signals:
            batches       = []
            total_tickers = 0
            logger.warning("風控熔斷中，本次跳過批次掃描（Step 2/3）")
        else:
            batches       = get_scan_batches(batch_size=SYSTEM["scan_batch_size"])
            total_tickers = sum(len(b) for b in batches)
            logger.info(f"共 {total_tickers} 檔，分 {len(batches)} 批")

        # 3. 批次掃描
        logger.info("Step 3/5: 開始批次掃描...")
        all_signals   = []
        scanned_count = 0
        error_count   = 0

        # ★ 修正：2026-09-03——原本這裡是完全序列化：一檔一檔掃、每檔掃完固定
        # sleep(scan_delay_sec=1.5秒)，860 檔光是這個刻意的延遲就佔了約 21.5
        # 分鐘，加上 fetch_all_timeframes() 內部週/日/時三個時間週期之間也各
        # sleep 0.3 秒，是全市場掃描要跑快 40 分鐘的主因（實測延遲佔比超過
        # 一半，真正花在對外部 API 請求/運算的時間反而是少數）。改成有限並行：
        # 同一批次內最多同時處理 _SCAN_CONCURRENCY 檔，每檔之間仍保留一個小的
        # 啟動間隔（SYSTEM["scan_delay_sec"]，已同步調降，見 config.py 說明），
        # 避免瞬間對 yfinance/TWSE 打出
        # 一大串同時發生的請求（這個專案先前就吃過 Yahoo/TPEx bot 防護的虧，
        # 這裡刻意保守，不做無限制平行）。每檔本身仍然透過
        # _scan_single_with_timeout 內部的獨立逾時保護（25秒不回應就放棄），
        # 所以就算某一檔真的卡住，也不會拖住整批、更不會拖住整個 process
        # （跟先前修的全站凍結是同一個保護機制，這裡只是把它包進並行工作）。
        _SCAN_CONCURRENCY = 4
        for batch_idx, batch in enumerate(batches):
            logger.info(f"批次 {batch_idx+1}/{len(batches)}（{len(batch)} 檔，同時 {_SCAN_CONCURRENCY} 檔）")
            with concurrent.futures.ThreadPoolExecutor(max_workers=_SCAN_CONCURRENCY) as pool:
                futures = {}
                for ticker in batch:
                    fut = pool.submit(self._scan_single_with_timeout, ticker, market_overview,
                                       fetch_all_timeframes, fetch_stock_institutional,
                                       generate_signal_tw, get_stock_info)
                    futures[fut] = ticker
                    time.sleep(SYSTEM["scan_delay_sec"])
                for fut in concurrent.futures.as_completed(futures):
                    ticker = futures[fut]
                    try:
                        sig = fut.result()
                        if sig:
                            all_signals.append(sig)
                        scanned_count += 1
                    except concurrent.futures.TimeoutError:
                        error_count += 1
                        logger.warning(f"[{ticker}] 掃描逾時（{self._SCAN_SINGLE_TIMEOUT_SEC}秒），放棄這檔繼續下一檔")
                    except Exception as e:
                        error_count += 1
                        logger.warning(f"[{ticker}] 掃描錯誤: {e}")
            if batch_idx < len(batches) - 1:
                time.sleep(2)
            # ★ 修正：2026-09-03——每批次結束主動觸發一次 gc，儘早回收
            # yfinance/pandas 呼叫產生的暫時物件，降低 512MB 方案上長時間
            # 掃描（30+ 分鐘、1000+ 檔）累積的記憶體壓力。
            gc.collect()

        # 4. 過濾排序
        logger.info("Step 4/5: 過濾與排序...")
        # ★ 新增：2026-09-16——使用者要求逐一核對歷史訊號實際結果時發現：群益台灣
        # 精選高息(00919.TW) 在 09-10、09-14、09-15 被連續推薦了三次 buy，全友
        # (2305.TW) 09-09、09-11、09-14、09-15 被推薦了四次——不是系統認為這檔
        # 特別好才連續強調，是舊評分公式讓大量候選同分，「今天恰好又擠進前2名」
        # 純屬巧合；就算評分公式已經修正，同一檔標的只要連續幾天都符合門檻，
        # 沒有機制阻止它每天都被重複推播，使用者等於在同一檔還沒平倉的部位上
        # 被反覆提醒「進場」，容易誤以為是三個獨立、加倍確認的機會，實際上是
        # 同一個部位的風險被重複計入。這裡在排序前先排除「目前已有未平倉/未結算
        # 訊號」的標的，同一檔要等前一筆訊號觸及停損/停利/逾期平倉後，才會再次
        # 出現在候選名單中。
        try:
            active_tickers = {s.get("ticker") for s in store.get_pending_signals()}
        except Exception as e:
            logger.warning(f"取得未平倉訊號清單失敗（不影響本次掃描繼續）: {e}")
            active_tickers = set()
        filtered = self._filter_and_rank(all_signals, market_overview, active_tickers)
        final_signals = filtered[:TELEGRAM_CONFIG["max_signals_per_day"]]

        self.signals_today = final_signals
        self.scan_count   += 1
        self.last_scan_at  = datetime.now(timezone.utc).isoformat()

        for sig in final_signals:
            store.save_signal(sig)

        stats = {
            "scanned":       scanned_count,
            "errors":        error_count,
            "signals_found": len(all_signals),
            "signals_sent":  len(final_signals),
            "duration_min":  round((time.time() - start_time) / 60, 1),
            "duration_sec":  round(time.time() - start_time, 1),
        }
        store.save_scan_history(stats)

        # 5. 推播
        logger.info("Step 5/5: 推播訊號...")
        self._push_signals(final_signals, market_overview, stats)

        logger.info(
            f"═══ 掃描完成 ═══\n"
            f"  掃描：{scanned_count} 檔（錯誤：{error_count}）\n"
            f"  發現：{len(all_signals)} → 篩選後：{len(final_signals)}\n"
            f"  耗時：{stats['duration_sec']} 秒"
        )
        return stats

    def _resolve_pending_signals(self):
        """
        ★ 新增：2026-09-03——稽核程式碼時發現 state_store.update_signal_result()／
        risk_manager.record_signal_loss() 兩個函式都定義了，但整個專案裡從來沒有
        任何地方呼叫過。實際後果：
          1. 網站首頁跟 /api/performance 顯示的「勝率」，因為資料庫裡每一筆訊號的
             status 永遠停在 'active'、result 永遠停在 'pending'，
             get_performance_summary() 算出來的 closed 永遠是 0，勝率永遠顯示 0%——
             不是策略真的沒有任何一筆訊號到價，是這個數字從頭到尾沒有被算過。
          2. risk_manager.check_daily_loss_limit()「今日虧損超過帳戶 6% 就停止產生
             新訊號」這個熔斷保護，因為虧損從沒被記錄過，永遠不會觸發，形同虛設。
        這裡在每次全市場掃描開始前，把資料庫裡還沒平倉的舊訊號抓出來，用該檔股票
        「訊號產生日之後」的日線最高/最低價，判斷有沒有先觸及停損、或先觸及
        停利一/二/三，用第一次發生的那天結算平倉（同一天內停損跟停利都被觸及時，
        保守優先判定停損，跟 backtester.py 既有的回測邏輯一致，避免高估績效）。
        找到就寫回資料庫、算真實損益（含手續費/證交稅，跟回測用同一套 calc_tw_pnl），
        虧損的話計入當日虧損上限，並且推播一則簡短通知給機主，讓機主知道自己追蹤的
        某檔訊號已經到價，不用自己每天盯盤核對。
        """
        from state_store import store
        from data_fetcher import fetch_ohlcv
        from backtester   import calc_tw_pnl

        pending = store.get_pending_signals()
        if not pending:
            return
        resolved = 0
        for sig in pending:
            try:
                ticker = sig.get("ticker")
                direction = sig.get("direction")
                sl, tp1, tp2, tp3 = sig.get("stop_loss"), sig.get("tp1"), sig.get("tp2"), sig.get("tp3")
                if not ticker or not direction or not sl:
                    continue
                gen_date = (sig.get("generated_at") or "")[:10]
                try:
                    data = self._call_with_timeout(self._SCAN_SINGLE_TIMEOUT_SEC, fetch_ohlcv, ticker, "daily")
                except concurrent.futures.TimeoutError:
                    logger.warning(f"_resolve_pending_signals: {ticker} 資料抓取逾時，跳過本次結算")
                    continue
                if not data:
                    continue
                dates, highs, lows = data.get("dates", []), data.get("highs", []), data.get("lows", [])
                hit_this_signal = False
                for i, d in enumerate(dates):
                    if not d or d <= gen_date:
                        continue  # 只看訊號產生「之後」的K棒，當天本身不算平倉
                    hi, lo = highs[i], lows[i]
                    hit_sl  = (direction == "buy" and lo <= sl) or (direction == "sell" and hi >= sl)
                    hit_tp3 = bool(tp3) and ((direction == "buy" and hi >= tp3) or (direction == "sell" and lo <= tp3))
                    hit_tp2 = bool(tp2) and ((direction == "buy" and hi >= tp2) or (direction == "sell" and lo <= tp2))
                    hit_tp1 = bool(tp1) and ((direction == "buy" and hi >= tp1) or (direction == "sell" and lo <= tp1))
                    if not (hit_sl or hit_tp3 or hit_tp2 or hit_tp1):
                        continue
                    if hit_sl:    result, close_price = "sl",  sl
                    elif hit_tp3: result, close_price = "tp3", tp3
                    elif hit_tp2: result, close_price = "tp2", tp2
                    else:         result, close_price = "tp1", tp1
                    entry  = sig.get("entry_price") or sig.get("current_price") or close_price
                    shares = sig.get("suggested_lots") or 1  # 欄位名稱歷史遺留，實際存的是股數
                    pnl     = calc_tw_pnl(entry, close_price, direction, shares)
                    pnl_pct = round(pnl / (entry * shares) * 100, 2) if entry and shares else 0
                    store.update_signal_result(sig["id"], result, close_price, pnl, pnl_pct)
                    if pnl < 0:
                        from risk_manager import record_signal_loss
                        record_signal_loss(pnl)
                    resolved += 1
                    hit_this_signal = True
                    try:
                        from telegram_bot import send_alert
                        result_zh = {"sl": "🔴 停損", "tp1": "✅ 停利一", "tp2": "✅ 停利二", "tp3": "🎯 停利三"}.get(result, result)
                        send_alert(
                            f"{sig.get('name','')}（{sig.get('code','')}）{result_zh}\n"
                            f"成交價 {close_price:.2f}｜損益 {pnl:+,.0f} 元（{pnl_pct:+.1f}%）",
                            "warning" if result == "sl" else "info",
                        )
                    except Exception as e:
                        logger.warning(f"_resolve_pending_signals 通知失敗 {sig.get('id')}: {e}")
                    break
                # ★ 新增：2026-09-16——CIRCUIT_BREAKER.signal_expire_days（預設3天）先前
                # 只是 config 裡定義的一個數字，從來沒有任何程式碼真的檢查它，導致沒觸及
                # 停損停利的舊訊號會永遠留在 pending 清單裡，state_store.get_pending_signals()
                # 隨時間無限增長，每次掃描前的結算階段耗時也跟著線性變慢（這是 Agent 稽核
                # 抓出的高優先度問題）。這裡補上真正的逾期判斷：訊號產生已超過 expire_days
                # 天、期間內都沒有觸及停損/任何停利，就強制以「最新收盤價」平倉結算，
                # 標記 result='expired'（不計入勝率的贏/輸，backtester/get_performance_summary
                # 的勝率算式只認 tp*/sl，expired 不會被誤記成任何一種），確保 pending 清單
                # 跟今日虧損上限的計算都反映真實現況，而不是被早就過期的舊訊號撐大。
                if not hit_this_signal and gen_date:
                    try:
                        gen_dt = datetime.strptime(gen_date, "%Y-%m-%d")
                    except ValueError:
                        gen_dt = None
                    expire_days = CB.get("signal_expire_days", 3)
                    if gen_dt and (datetime.now(timezone.utc).replace(tzinfo=None) - gen_dt).days >= expire_days:
                        closes = data.get("closes", [])
                        last_close = closes[-1] if closes else (sig.get("entry_price") or sig.get("current_price") or 0)
                        entry  = sig.get("entry_price") or sig.get("current_price") or last_close
                        shares = sig.get("suggested_lots") or 1
                        pnl     = calc_tw_pnl(entry, last_close, direction, shares) if entry else 0
                        pnl_pct = round(pnl / (entry * shares) * 100, 2) if entry and shares else 0
                        store.update_signal_result(sig["id"], "expired", last_close, pnl, pnl_pct)
                        if pnl < 0:
                            from risk_manager import record_signal_loss
                            record_signal_loss(pnl)
                        resolved += 1
                        logger.info(
                            f"_resolve_pending_signals: {sig.get('name','')}（{ticker}）"
                            f"超過 {expire_days} 天未觸及停損/停利，強制以現價 {last_close:.2f} 平倉（expired）"
                        )
            except Exception as e:
                logger.warning(f"_resolve_pending_signals {sig.get('id')}: {e}")
        if resolved:
            logger.info(f"平倉結算：{resolved} 筆訊號已達停損/停利")

    _SCAN_SINGLE_TIMEOUT_SEC = 25
    _MARKET_OVERVIEW_TIMEOUT_SEC = 45

    @staticmethod
    def _call_with_timeout(timeout_sec, fn, *args, **kwargs):
        """
        ★ 新增：2026-09-03——這次修正上線後實測手動觸發一次全市場掃描，跑到約
        批次 15/18 附近，Render 服務整個凍結超過 30 分鐘，連 /health 都打不通，
        等於「單一次資料抓取卡住」直接讓整個網站掛掉。因為 gunicorn 只有 1 個
        worker，data_fetcher.py 裡好幾個 yfinance 呼叫完全沒有設定任何逾時
        （yf.Ticker(...).history(...) 沒有 timeout 參數），一旦某次請求連線
        建立了但對方遲遲不回應（不是連線被拒絕、不是逾時例外——是真的卡住不動、
        不拋錯、log 也完全靜默），Python 會無限期等待，連帶整個 process 都被
        卡死。這裡提供一個共用的逾時包裝：把呼叫丟到獨立執行緒跑，最多等
        timeout_sec 秒，逾時就直接放棄、拋出 TimeoutError 讓呼叫端可以繼續
        下一步，不等底層卡住的執行緒真的結束（它是背景執行緒，不會阻止
        process 退出，最壞情況只是背景多一條卡住的執行緒，總比整個網站掛掉好）。
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, *args, **kwargs)
            return future.result(timeout=timeout_sec)
        finally:
            executor.shutdown(wait=False)

    def _scan_single_with_timeout(self, ticker, market_overview, fetch_tf, fetch_inst, gen_signal, get_info) -> Optional[Dict]:
        return self._call_with_timeout(
            self._SCAN_SINGLE_TIMEOUT_SEC, self._scan_single,
            ticker, market_overview, fetch_tf, fetch_inst, gen_signal, get_info,
        )

    def _scan_single(self, ticker, market_overview, fetch_tf, fetch_inst, gen_signal, get_info) -> Optional[Dict]:
        stock_info = get_info(ticker)
        if not stock_info: return None
        tf_data = fetch_tf(ticker)
        if not tf_data: return None
        code = stock_info.get("code", ticker.replace(".TW","").replace(".TWO",""))
        inst_data = fetch_inst(code)
        return gen_signal(ticker, stock_info, tf_data, market_overview, inst_data)

    def _check_market_status(self, market_overview: Dict):
        # ★ 修正：2026-08-30——market_status/twii_chg 缺資料時原本直接安靜當成
        #   "normal"/0%，跟「大盤真的正常」沒有任何區別，會讓下面「大盤重挫只
        #   掃空單」「大盤偏弱提高門檻」這兩層保護在資料層失敗時悄悄失效。
        if "market_status" not in market_overview:
            logger.warning("_check_market_status: market_overview 缺少 market_status，當作 normal 處理")
        status   = market_overview.get("market_status","normal")
        twii_chg = market_overview.get("index",{}).get("twii",{}).get("chg",0)
        # 每次掃描先重置回基準值，才不會跨天永久疊加（見上方模組層級註解）
        THRESH["min_score"] = _BASE_MIN_SCORE
        if status == "stop":
            logger.warning(f"大盤重挫 {twii_chg:.1f}%，本次只掃空單")
        elif status == "caution":
            logger.warning(f"大盤偏弱 {twii_chg:.1f}%，提高門檻")
            THRESH["min_score"] = min(75, _BASE_MIN_SCORE + 5)

    def _filter_and_rank(self, signals: List[Dict], market_overview: Dict, active_tickers: Optional[set] = None) -> List[Dict]:
        if not signals: return []
        if active_tickers:
            before = len(signals)
            signals = [s for s in signals if s.get("ticker") not in active_tickers]
            removed = before - len(signals)
            if removed:
                logger.info(f"_filter_and_rank: 排除 {removed} 檔已有未平倉訊號的重複標的（見上方新增說明）")
        if market_overview.get("market_status") == "stop":
            signals = [s for s in signals if s["direction"] == "sell"]
        # 同產業去重（只留最高分）
        # ★ 修正：2026-08-30——今天稽核程式碼時抓到一個還沒真的發生過、但影響非常大的
        #   潛在 bug：sig["sector"] 來自 stock_universe.py 的 _fetch_sector_info()，那個
        #   函式打 TWSE 的產業分類 API，今天在這個 sandbox 裡就實際看過同一類 TWSE 端點
        #   回 404 好幾次(處置/注意股票清單)，沒有理由未來這個端點不會遇到一樣的狀況。
        #   如果那次請求失敗，原本的寫法會讓「今天全部股票的 sector 都是空字串→其他」，
        #   下面這段同產業去重邏輯就會把每一檔股票都歸類進同一個 "其他" bucket，等於
        #   「只留全市場分數最高的一檔訊號，其他全部被去重掉」——不會報錯、不會有任何
        #   log，看起來就像「今天訊號很少」，但其實是資料層失敗、去重邏輯被誤用造成的資料
        #   遺失。這裡加一層防呆：如果去重前的所有訊號幾乎都落在同一個 sector（代表 sector
        #   資訊根本沒有真的區分開來，不是「剛好同產業訊號真的很多」），直接跳過同產業
        #   去重、記一行警告，不要讓一個「產業分類去重」的 UX 優化功能，在資料失敗時
        #   變成「只剩一檔訊號」的資料遺失 bug。
        distinct_sectors = {sig.get("sector", "其他") for sig in signals}
        if len(signals) > 1 and len(distinct_sectors) <= 1:
            logger.warning(
                f"_filter_and_rank: {len(signals)} 檔訊號的 sector 全部相同（{distinct_sectors}），"
                f"可能是產業分類資料抓取失敗，跳過同產業去重，改為全部保留"
            )
            combined = list(signals)
        else:
            sector_best: Dict[str, Dict] = {}
            for sig in signals:
                sec = sig.get("sector","其他")
                if sec not in sector_best or sig["score"] > sector_best[sec]["score"]:
                    sector_best[sec] = sig
            combined = list(sector_best.values())
        combined.sort(key=lambda x: x["score"], reverse=True)
        return combined

    def _push_signals(self, signals: List[Dict], market_overview: Dict, stats: Dict):
        # ★ 修正：2026-09-16——這是這次「訊號沒有及時傳到 TG」問題稽核出的最關鍵 bug：
        # 原本整個函式只包在同一個 try/except 裡，代表「每日總結報告」格式化失敗，
        # 或者「任何一檔」訊號推播失敗（Telegram API 逾時、429 限流、網路錯誤……），
        # 都會讓 for 迴圈直接被例外中斷，當天排在後面、原本完全正常的訊號全部
        # 不會送出，而且沒有任何警示——使用者只會發現「今天 Telegram 什麼都沒收到」，
        # 卻無法分辨是「今天真的沒訊號」還是「系統生成了訊號但推播中途死掉」。
        # 這裡把「總結報告」和「每一檔訊號」都各自包一層 try/except，任何一個失敗
        # 只跳過那一個、不影響其他訊號，並且失敗時額外呼叫 send_alert 通知機主，
        # 讓失敗變成看得到的警示，而不是沉默漏推。
        try:
            from telegram_bot import push_signal, send_daily_report, send_alert
        except Exception as e:
            logger.error(f"_push_signals: 無法載入 telegram_bot，本次全部訊號未推播: {e}", exc_info=True)
            return

        try:
            send_daily_report(signals, market_overview, stats)
        except Exception as e:
            logger.error(f"_push_signals: 每日總結報告推播失敗: {e}", exc_info=True)
            try:
                send_alert(f"⚠️ 每日總結報告推播失敗：{e}", "warning")
            except Exception as e2:
                logger.error(f"_push_signals: 總結報告失敗警示也送不出去: {e2}")

        time.sleep(1)
        push_failures = []
        for sig in signals:
            try:
                push_signal(sig)
            except Exception as e:
                push_failures.append(sig.get("name") or sig.get("code") or sig.get("ticker") or "?")
                logger.error(f"_push_signals: 訊號推播失敗 {sig.get('ticker')}: {e}", exc_info=True)
            time.sleep(0.5)

        if push_failures:
            try:
                send_alert(
                    f"⚠️ 今日有 {len(push_failures)} 檔訊號推播失敗：{'、'.join(push_failures)}\n"
                    f"請至網站確認這幾檔的進出場資訊",
                    "warning",
                )
            except Exception as e:
                logger.error(f"_push_signals: 推播失敗警示本身也送不出去: {e}")

    def get_today_signals(self) -> List[Dict]:
        return self.signals_today

    def get_status(self) -> Dict:
        return {
            "scan_count":   self.scan_count,
            "last_scan_at": self.last_scan_at,
            "signal_count": len(self.signals_today),
            "signals":      self.signals_today,
            "is_scanning":  self.is_scanning,
        }


scanner = TWScanEngine()
