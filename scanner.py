"""
scanner.py — 全市場掃描引擎 v1.0
每日 16:30 盤後觸發，批次掃描 1000+ 檔台股
"""
import time, logging, threading, concurrent.futures, gc
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

from config import SIGNAL_THRESHOLDS as THRESH, CIRCUIT_BREAKER as CB, SYSTEM, TELEGRAM_CONFIG, \
    CORRELATION_GROUPS, MAX_PER_CORRELATION_GROUP, is_earnings_season


def _correlation_group_key(sector: str, ticker: str = "") -> str:
    """★ 新增：2026-09-16——把 sig["sector"] 這個字串對應到 CORRELATION_GROUPS
    裡定義的相關性群組（見 config.py 註解）。不在任何群組內的 sector 視為
    自成一組，不會跟其他未分組的 sector 共用曝險上限。

    ★ 修正：2026-09-18——使用者反映「這幾天訊號一直失敗/一直是0個」，稽核發現
    2026-09-17 全市場掃描 815 檔、193 檔通過評分門檻，_filter_and_rank() 卻把
    193 檔全部濾成 0 檔推播。追出來的高度可疑成因：sector 抓取失敗時（
    stock_universe._fetch_sector_info() 打 TWSE 產業分類 API，這個專案已經
    證實這類 TWSE 端點會 404/失敗，見 2026-08-30 同產業去重那次的對應防呆）
    原本的寫法會讓所有「不明產業」的候選全部落進同一個共用的「其他」桶，
    跟 MAX_PER_CORRELATION_GROUP（=2）比較——如果剛好有 2 筆既有持倉的
    sector 資料本身就是空字串（例如 ETF 類标的常常沒有明確產業分類），這個
    「其他」桶在還沒看任何一檔新候選之前就已經滿了，之後所有 sector 同樣
    抓取失敗的新候選，不管分數多高，全部會被判定「曝險已滿」直接跳過——而
    這個「曝險已滿」的判斷其實是假的，因為我們根本不知道這些標的實際上是
    不是真的同產業/高度連動，只是「不知道」而已。「不知道」不該被當成
    「已知且相同」來共用同一個曝險上限。這裡把不明 sector 的 fallback 改成
    用 ticker 讓每一檔自成一組（等於對「不明產業」的標的完全不設相關性上限
    ——沒有可靠的分類資訊時，寧可不限制，也不要用假分組誤傷彼此完全無關的
    候選）。"""
    for grp in CORRELATION_GROUPS:
        if sector in grp:
            return "|".join(grp)
    # ★ 修正：2026-09-24——這裡原本只把「空字串」sector 視為不明產業、給每檔
    # 自己的 key，但 stock_universe.build_universe() 實際上從來不會塞空字串
    # ——sector_map.get(s["code"], "其他") 找不到分類時，預設值就是字面上的
    # 「其他」這個字串（不是空字串）。結果就是真正常見的情況（TWSE 產業分類
    # API 沒有 ETF/部分 OTC 標的的資料，預設落到「其他」）完全沒被這個 if
    # 擋到，這些互不相干的標的還是全部共用同一個「其他」曝險桶，跟這次修正
    # 想解決的問題一模一樣，只是換了個字串觸發不到判斷式。這裡把「其他」也
    # 視為不明產業一併處理。
    if not sector or sector == "其他":
        return f"__unknown_sector__:{ticker}" if ticker else "其他"
    return sector

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
        # ★ 修正：2026-09-18——last_scan_at 原本只存在記憶體裡，process 一
        # 重啟（不管是部署、還是 2026-09-03/2026-09-18 那種被平台強制重啟的
        # 情況）就歸零，job_scan_watchdog() 因此在重啟後的第一次檢查會誤判
        # 成「最後一次掃描時間：從未執行過」——即使前幾天掃描明明都正常跑完，
        # 只是process換了一個新的而已。這裡啟動時改成從 DB 的 meta 表讀回
        # 上次真正掃描完成的時間，讓這個狀態能跨越 process 重啟存活，
        # watchdog 的警示訊息才會準確反映「真的有多久沒掃描」，而不是「這個
        # process活了多久」。用 try/except 包住，DB 在啟動當下還沒準備好時
        # 不影響其餘啟動流程。
        self.last_scan_at:  str        = ""
        try:
            from state_store import store
            self.last_scan_at = store.get_meta("last_scan_at", "") or ""
        except Exception as e:
            logger.warning(f"TWScanEngine.__init__: 讀取 last_scan_at 失敗（不影響啟動）: {e}")
        self.scan_errors:   List[str]  = []
        # ★ 修正：2026-09-03——原本 run_daily_scan() 沒有任何「正在掃描中」的鎖，
        # 如果排程的每日掃描（16:30）跟手動觸發的 /api/scan/force（或使用者連點
        # 兩次網站上的「立即掃描」按鈕）同時執行，兩個掃描會同時打相同的資料來源
        # API、同時寫入 self.signals_today / SQLite，訊號可能重複推播兩次到
        # Telegram，且更容易把上游 API 打到限流。這裡加一個簡單的執行鎖，掃描
        # 進行中時直接跳過，不會排隊等待（避免使用者連點時卡住一堆執行緒）。
        self._scan_lock:    threading.Lock = threading.Lock()
        # ★ 新增：2026-09-16——使用者質疑「盤中完全沒有掃描」是否合理。查證業界
        # 波段(swing)作法後，「盤前+盤後各檢查一次」本身是常見、正常的節奏，並非
        # 這裡設計有誤；但那個節奏的前提是「使用者在券商那邊真的掛了停損限價單」，
        # 券商會在盤中價格觸及時自動執行，不需要系統盯盤。這個專案目前只是「訊號
        # 推播」，不會幫使用者自動下單（FUBON_CONFIG.auto_trade=False），使用者是否
        # 真的在自己券商那邊掛了對應的停損單，系統無從得知也無法強制。也就是說，
        # 如果使用者沒有另外掛停損單、只靠這個 Bot 的訊息操作，原本的設計會讓「今天
        # 盤中已經跌破停損」這件事，要等到「明天」盤後掃描（用明天的日K高低點回頭
        # 判斷）才會發現、推播——足足慢了一個交易日，這段時間帳戶已經暴露在遠超過
        # 原訂 2% 的風險之下都不會有任何提醒。這裡加一層「盤中安全網」：只做價格比對
        # +即時警示，不寫入資料庫、不影響 _resolve_pending_signals() 既有的權威結算
        # 邏輯（那個仍然用日K高低點判斷，維持跟回測一致的績效計算方式），純粹是讓
        # 使用者能在當天、而不是隔天才知道「這筆訊號已經到價」。同一筆訊號同一天只
        # 警示一次，避免每小時洗版。
        self._intraday_alerted: Dict = {"date": "", "ids": set()}

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
            _pending = store.get_pending_signals()
            active_tickers = {s.get("ticker") for s in _pending}
            # ★ 新增：2026-09-16——連同已在手上、尚未平倉的訊號的 sector 一起
            # 傳給 _filter_and_rank()，讓相關性群組上限（MAX_PER_CORRELATION_GROUP）
            # 是「含既有持倉」的真實曝險計算，而不是只看今天新掃出來的候選，
            # 否則今天新增2檔半導體訊號時，系統不會知道你手上可能已經有2檔
            # 半導體部位還沒平倉，等於上限形同虛設。
            # ★ 修正：2026-09-18——原本只傳 sector 字串，_correlation_group_key()
            # 拿不到 ticker，sector 抓取失敗（空字串）的既有持倉全部會被歸進
            # 同一個共用的「其他」桶，見 _correlation_group_key() 說明的那個
            # bug。這裡改傳完整的 _pending（含 ticker），讓 fallback 能用
            # ticker 讓每筆不明產業的持倉自成一組。
            active_positions = _pending
        except Exception as e:
            logger.warning(f"取得未平倉訊號清單失敗（不影響本次掃描繼續）: {e}")
            active_tickers = set()
            active_positions = []
        filtered = self._filter_and_rank(all_signals, market_overview, active_tickers, active_positions)
        final_signals = filtered[:TELEGRAM_CONFIG["max_signals_per_day"]]

        self.signals_today = final_signals
        self.scan_count   += 1
        self.last_scan_at  = datetime.now(timezone.utc).isoformat()
        # ★ 修正：2026-09-18——同步寫回 DB meta 表，見 __init__() 說明，讓
        # job_scan_watchdog() 在 process 重啟後也能讀到「真正」的上次掃描
        # 時間，不會誤報「從未執行過」。
        try:
            store.set_meta("last_scan_at", self.last_scan_at)
        except Exception as e:
            logger.warning(f"寫回 last_scan_at 失敗（不影響本次掃描結果）: {e}")

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

    def check_intraday_price_alerts(self):
        """
        ★ 新增：2026-09-16——盤中安全網，見 __init__ 裡的說明。設計成排程呼叫
        （09:00-13:30 盤中；★ 2026-09-19 起改為每30分鐘一次，見 app.py
        setup_scheduler() 說明），只做「現價 vs 已記錄的 SL/TP」比對跟即時
        Telegram 警示，完全不寫資料庫、不呼叫 update_signal_result()——真正的
        結算（含損益計算、寫回 status=closed）仍然只在 _resolve_pending_signals()
        裡、用隔天的日K高低點做，兩者不會互相干擾或重複計入績效。

        ★ 為什麼「盤中掃描」不是「盤中重新找新訊號」：這個波段策略的訊號（指標、
        進出場價位）都是用「已經走完收盤的日K」算出來的。盤中的日K還沒收盤，
        當下看到的高低點、均線、乖離都還會隨盤中價格持續變動——用這種還沒定案的
        殘缺K棒去跑訊號邏輯，同一檔股票很可能上午算出買進訊號、下午行情一動又
        不符合條件，一天內訊號自己打自己臉（來回洗），這比「盤中沒有新訊號」更
        傷。所以目前的作法是：現有已追蹤部位的即時狀態做到隨時可見（本函式），
        但「找新的進場訊號」仍然只在收盤後用當天定案的日K跑一次
        （job_daily_scan，16:30 CST）。要盤中也能找新訊號，需要先設計一套專門
        給「未收盤K棒」用的訊號邏輯，不能直接沿用現有日K邏輯。
        """
        from state_store import store
        from data_fetcher import fetch_batch_current_prices
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._intraday_alerted.get("date") != today:
            self._intraday_alerted = {"date": today, "ids": set()}
        pending = store.get_pending_signals()
        if not pending:
            logger.info("check_intraday_price_alerts: 目前無未平倉訊號，跳過本次盤中檢查")
            return
        tickers = list({s["ticker"] for s in pending if s.get("ticker")})
        try:
            prices = fetch_batch_current_prices(tickers)
        except Exception as e:
            logger.warning(f"check_intraday_price_alerts: 批次抓現價失敗: {e}")
            return
        # ★ 新增：2026-09-18——使用者反映「盤中資訊根本沒有跳出來」。稽核發現這個
        # 函式原本「沒異常就完全沉默」：抓不到現價、現價字典整包是空的、或現價
        # 抓到了但沒有任何一檔真的觸及SL/TP，三種情況在log裡完全一樣（什麼都不
        # 印），事後沒辦法分辨「今天盤中真的很平靜」還是「這個安全網其實根本沒
        # 在運作」。這裡不管有沒有觸發警示，都先記清楚「要查幾檔、實際查到幾檔」，
        # 之後才好判斷問題出在資料源還是真的沒事發生。
        logger.info(f"check_intraday_price_alerts: 追蹤 {len(tickers)} 檔，成功取得現價 {len(prices)} 檔")
        if not prices:
            logger.warning(
                "check_intraday_price_alerts: fetch_batch_current_prices 回傳空結果，"
                "本次無法比對現價（可能是資料源逾時/被擋，不代表盤中真的沒有變化）"
            )
            return
        missing = [t for t in tickers if t not in prices]
        if missing:
            logger.warning(f"check_intraday_price_alerts: 以下 {len(missing)} 檔沒有抓到現價，本次未納入比對: {missing}")
        alerted = 0
        for sig in pending:
            sig_id = sig.get("id")
            ticker = sig.get("ticker")
            if not sig_id or sig_id in self._intraday_alerted["ids"] or ticker not in prices:
                continue
            price = prices[ticker]
            direction = sig.get("direction")
            sl, tp1 = sig.get("stop_loss"), sig.get("tp1")
            hit_sl = sl and ((direction == "buy" and price <= sl) or (direction == "sell" and price >= sl))
            hit_tp1 = tp1 and ((direction == "buy" and price >= tp1) or (direction == "sell" and price <= tp1))
            if not (hit_sl or hit_tp1):
                continue
            self._intraday_alerted["ids"].add(sig_id)
            alerted += 1
            try:
                from telegram_bot import send_alert
                if hit_sl:
                    send_alert(
                        f"🔴 盤中警示：{sig.get('name','')}（{sig.get('code','')}）現價 {price:.2f} 已觸及停損 {sl:.2f}\n"
                        f"若尚未在券商端設定停損單，請儘速確認部位", "warning")
                else:
                    send_alert(
                        f"✅ 盤中警示：{sig.get('name','')}（{sig.get('code','')}）現價 {price:.2f} 已觸及 TP1 {tp1:.2f}\n"
                        f"可考慮依原計畫分批出場", "info")
            except Exception as e:
                logger.warning(f"check_intraday_price_alerts 通知失敗 {sig_id}: {e}")
        if alerted:
            logger.info(f"check_intraday_price_alerts: 本次盤中檢查發出 {alerted} 則警示")

        # ★ 新增：2026-09-18——見上方說明，使用者反映盤中除了早上「請留意止損位」
        # 這句制式提醒外，整天完全看不到任何實際數字，只有真的觸及SL/TP才會
        # 收到訊息。原本固定只在中午12:00送一次現況總覽。
        # ★ 調整：2026-09-19——使用者反映「隨時盤中都要有掃描」，中午一次頻率不夠。
        # 現在檢查頻率已提高到每30分鐘一次（見本函式開頭說明），但現況總覽若
        # 也跟著每30分鐘推播（一天10次）容易變成疲勞轟炸；改成整點才送
        # （9:00/10:00/11:00/12:00/13:00）另外收盤前的13:30固定也送一次，
        # 一天共6次，30分鐘的檢查頻率仍然照跑、只是不是每次都額外推播總覽——
        # SL/TP真的被觸及的警示（上面那段）則不受此限制，任何一次30分鐘檢查
        # 發現到價都會立刻推播。
        now_tw = datetime.now(timezone.utc) + timedelta(hours=8)
        if now_tw.minute == 0 or (now_tw.hour == 13 and now_tw.minute == 30):
            try:
                from telegram_bot import send_intraday_digest
                send_intraday_digest(pending, prices)
            except Exception as e:
                logger.warning(f"check_intraday_price_alerts: 盤中現況總覽推播失敗: {e}")

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
        # ★ 新增：2026-09-16——B1財報密集期保護（見 config.py is_earnings_season()
        # 註解）。跟大盤 caution 的門檻提升「取較大值、不相加」——過去
        # THRESH["min_score"] += 5 曾經造成永久疊加的重大bug（見上方模組層級
        # 註解），這裡刻意沿用同一個教訓：即使大盤 caution 跟財報密集期同時
        # 成立，也只提高一次門檻，不會讓兩個原因疊加出一個更誇張的門檻。
        bump = 5 if status == "caution" else 0
        earnings = is_earnings_season()
        if earnings["in_season"]:
            logger.warning(f"財報密集申報期（最近一個法定截止日 {earnings['deadline']}，"
                            f"剩 {earnings['days_left']} 天），提高門檻並於訊號中提示")
            bump = max(bump, 5)
        if bump:
            THRESH["min_score"] = min(75, _BASE_MIN_SCORE + bump)

    def _filter_and_rank(self, signals: List[Dict], market_overview: Dict, active_tickers: Optional[set] = None,
                          active_positions: Optional[List[Dict]] = None) -> List[Dict]:
        # ★ 新增：2026-09-18——使用者反映訊號連續好幾天是0個，稽核 09-17 的
        # log 發現「發現：193 → 篩選後：0」，但這個函式原本除了「相關性群組
        # 排除」跟「重複標的排除」各自的log，沒有印出每個階段各自的存活數量，
        # 事後很難判斷是哪一關把候選清空的。這裡在函式開頭跟每個階段後都印一次
        # 數量，之後同樣情況發生時，從log就能直接看出是哪一關的問題。
        logger.info(f"_filter_and_rank: 輸入 {len(signals)} 檔候選")
        if not signals: return []
        if active_tickers:
            before = len(signals)
            signals = [s for s in signals if s.get("ticker") not in active_tickers]
            removed = before - len(signals)
            if removed:
                logger.info(f"_filter_and_rank: 排除 {removed} 檔已有未平倉訊號的重複標的（見上方新增說明），剩 {len(signals)} 檔")
        if market_overview.get("market_status") == "stop":
            signals = [s for s in signals if s["direction"] == "sell"]

        # ★ 新增：2026-09-24——使用者要求波段訊號不能只看技術面，個股基本面
        # （月營收年增率）明顯衰退時要硬性排除，不管技術分數多高。放在同產業
        # 去重「之前」執行，避免一檔基本面地雷因為技術分數最高、先佔走該產業
        # 的去重名額，把真正該入選的同產業其他標的擠掉。找不到營收資料的標的
        # （新股、資料源當天失敗）一律不擋，理由見 fundamentals.py 說明。
        try:
            from fundamentals import fetch_monthly_revenue_map, check_fundamental_hard_filter
            revenue_map = fetch_monthly_revenue_map()
            before = len(signals)
            fundamentally_blocked = []
            kept = []
            for sig in signals:
                chk = check_fundamental_hard_filter(sig.get("code", ""), revenue_map)
                if chk["blocked"]:
                    fundamentally_blocked.append(f"{sig.get('ticker')}（{chk['reason']}）")
                else:
                    kept.append(sig)
            signals = kept
            if fundamentally_blocked:
                logger.info(f"_filter_and_rank: 基本面硬性過濾排除 {len(fundamentally_blocked)} 檔：{fundamentally_blocked}")
            logger.info(f"_filter_and_rank: 基本面過濾後剩 {len(signals)}/{before} 檔")
        except Exception as e:
            logger.warning(f"_filter_and_rank: 基本面過濾失敗（不影響本次掃描，本次跳過基本面過濾）: {e}")

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
            # ★ 修正：2026-09-24——稽核 2026-09-23 實際掃描log發現這一段還是有
            # 同一類bug：上面 08-30 的防呆只擋得住「幾乎每一檔都同一個sector」
            # 的極端情況（len(distinct_sectors)<=1）。但常見的實際情況是「有
            # 幾檔真的有明確產業分類，但其餘大多數（尤其ETF/部分未分類個股）
            # 全部落在stock_universe.build_universe()預設的「其他」」——這時
            # distinct_sectors 至少有2個值，防呆不會觸發，但「其他」這個桶
            # 底下可能塞了上百檔完全不相關的標的，下面這段「同sector只留最高
            # 分1檔」的邏輯會把它們當成同一產業、只留1檔，實測 2026-09-23
            # 135檔候選被這段壓到只剩2檔，是當天「篩選後只剩1個訊號」的主因
            # （不是相關性群組上限——那一關同一天只多濾掉1檔）。改成「其他」/
            # 空字串比照 _correlation_group_key() 的處理方式，用ticker讓每一
            # 檔自成一組，不再被錯誤地當成「同產業」去重。真正的產業集中度
            # 上限交給下面的相關性群組曝險上限（MAX_PER_CORRELATION_GROUP）
            # 處理，那一關本來就是為了這個目的存在、而且已經修過同樣的bug。
            sector_best: Dict[str, Dict] = {}
            for sig in signals:
                raw_sec = sig.get("sector", "其他")
                key = raw_sec if raw_sec and raw_sec != "其他" else f"__unknown_sector__:{sig.get('ticker','')}"
                if key not in sector_best or sig["score"] > sector_best[key]["score"]:
                    sector_best[key] = sig
            combined = list(sector_best.values())
        combined.sort(key=lambda x: x["score"], reverse=True)
        logger.info(f"_filter_and_rank: 同產業去重後剩 {len(combined)} 檔")

        # ★ 新增：2026-09-16——啟用 CORRELATION_GROUPS 相關性群組曝險上限（見
        # config.py 註解、_correlation_group_key()）。同產業去重只能擋掉
        # sector 字串完全相同的重複，擋不住「半導體」「AI概念」「電子零組件」
        # 這種高度連動、但字串不同的產業同時塞滿持倉上限。這裡先用既有持倉的
        # sector 把每個群組的計數初始化，再依分數高低依序納入新訊號，超過
        # MAX_PER_CORRELATION_GROUP 的群組直接跳過該訊號（分數較低的同群組
        # 訊號會被排除，不會影響其他群組）。
        # ★ 修正：2026-09-18——見 _correlation_group_key() 說明的重大 bug：
        # sector 空字串的既有持倉原本會被歸進共用的「其他」桶，可能還沒看
        # 任何新候選就把桶塞滿，導致之後所有 sector 同樣不明的新候選被誤判
        # 「曝險已滿」。這裡改用 _correlation_group_key(sector, ticker) 讓
        # 不明產業的持倉/候選各自獨立，不再共用假的曝險上限。
        group_counts: Dict[str, int] = {}
        for pos in (active_positions or []):
            key = _correlation_group_key(pos.get("sector", ""), pos.get("ticker", ""))
            group_counts[key] = group_counts.get(key, 0) + 1
        if group_counts:
            logger.info(f"_filter_and_rank: 既有持倉曝險群組初始計數 {group_counts}")

        final: List[Dict] = []
        excluded_by_group: List[str] = []
        for sig in combined:
            key = _correlation_group_key(sig.get("sector", ""), sig.get("ticker", ""))
            if group_counts.get(key, 0) >= MAX_PER_CORRELATION_GROUP:
                excluded_by_group.append(f"{sig.get('ticker')}({sig.get('sector')})")
                continue
            group_counts[key] = group_counts.get(key, 0) + 1
            final.append(sig)
        if excluded_by_group:
            logger.info(
                f"_filter_and_rank: 相關性群組曝險上限（同群組最多 {MAX_PER_CORRELATION_GROUP} 檔，"
                f"含既有持倉）排除 {len(excluded_by_group)} 檔訊號：{excluded_by_group}"
            )
        logger.info(f"_filter_and_rank: 最終輸出 {len(final)} 檔")
        return final

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
