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
        # ★ 修正：2026-10-05——使用者回報「今日訊號 Telegram 有推、網頁卻完全沒有」。
        # 根因：signals_today 只存在記憶體，Render 每次部署/重啟/休眠喚醒 process 就
        # 歸零，網頁讀的 /api/state 就變成 0 筆，但訊號早已寫進資料庫、TG 也早就推出去
        # 了。這裡啟動時從 DB 還原「最近一次掃描日」的訊號，讓網頁與 TG 看到同一份事實。
        try:
            from state_store import store as _st
            import json as _json
            recent = _st.get_recent_signals(limit=60, days_back=7)
            if recent:
                latest_day = (recent[0].get("generated_at") or "")[:10]
                restored = []
                for r in recent:
                    if (r.get("generated_at") or "")[:10] != latest_day:
                        continue
                    try:
                        d = _json.loads(r.get("raw_json") or "{}")
                    except Exception:
                        d = {}
                    if not d:
                        d = dict(r)
                    d["status"] = r.get("status", d.get("status", "active"))
                    d["result"] = r.get("result", d.get("result", "pending"))
                    restored.append(d)
                self.signals_today = restored
                logger.info(f"TWScanEngine: 從資料庫還原 {len(restored)} 筆最近一次掃描（{latest_day}）的訊號")
        except Exception as e:
            logger.warning(f"TWScanEngine.__init__: 還原今日訊號失敗（不影響啟動）: {e}")
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
                # ★ 修正：2026-09-29——見下面 _push_signals() 「今日已推播過一次
                # 盤後集結報告就跳過重複推播」的同一個修正說明——這則風控熔斷
                # 警示是同一次完整掃描流程裡、比 _push_signals 更早的一步，如果
                # 當天稍早已經整套跑過一次（不管是手動立即掃描還是排程/重啟
                # 重疊），這裡也會因為條件同樣成立而重複發一次一模一樣的熔斷
                # 警示——這正是使用者截圖裡「已有13個持倉，暫停新增」訊息連續
                # 出現兩次的根因。用同一個 daily_report_sent_date 旗標判斷「今天
                # 是否已經跑過一次完整報告」，是的話這則警示也一併跳過，不需要
                # 額外開一個旗標。
                already_reported_today = False
                try:
                    from state_store import store as _store
                    _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    already_reported_today = _store.get_meta("daily_report_sent_date", "") == _today
                except Exception as e:
                    logger.warning(f"風控熔斷通知：讀取今日是否已推播旗標失敗（{e}），保守起見照常推播")
                if already_reported_today:
                    logger.warning("風控熔斷通知：今天稍早已經推播過一次完整報告（含此警示的內容），本次視為重複觸發，跳過")
                else:
                    try:
                        from telegram_bot import send_alert
                        send_alert("🛑 風控熔斷，今日暫停產生新訊號\n" + "\n".join(block_msgs), "warning")
                    except Exception as e:
                        logger.warning(f"風控熔斷通知推播失敗: {e}")
        except Exception as e:
            logger.warning(f"risk_manager 熔斷檢查失敗（不影響本次掃描繼續）: {e}", exc_info=True)

        # 2. 品種清單
        logger.info("Step 2/5: 建立掃描清單...")
        # ★ 2026-10-05：風控熔斷時仍照常掃描，但只做「影子追蹤」(shadow.py) 累積學習資料，
        # 不產生、不推播任何實際訊號（見掃描後 all_signals 被清空）。
        batches       = get_scan_batches(batch_size=SYSTEM["scan_batch_size"])
        total_tickers = sum(len(b) for b in batches)
        if skip_new_signals:
            logger.warning("風控熔斷中：本次只做影子追蹤（記錄候選與對照組），不產生實際訊號")
        logger.info(f"共 {total_tickers} 檔，分 {len(batches)} 批")

        # 3. 批次掃描
        logger.info("Step 3/5: 開始批次掃描...")
        # 三大法人資料先抓一次（單一航班＋快取）；失敗時各檔直接略過法人加減分，不再每檔各等一次逾時
        try:
            from data_fetcher import fetch_institutional_flow, inst_status
            _inst = fetch_institutional_flow()
            logger.info(f"三大法人預載：{len(_inst)} 檔 {inst_status()}")
        except Exception as _e:
            logger.warning(f"三大法人預載失敗: {_e}")
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
        _SCAN_CONCURRENCY = 8
        try:
            import shadow
            shadow.begin_run("熔斷中僅影子追蹤" if skip_new_signals else "")
        except Exception as e:
            logger.warning(f"影子追蹤初始化失敗（不影響掃描）: {e}")
        with self._pre_lock:
            self._ctrl = []
            self._pre = {"skipped": 0, "passed": 0, "audited": 0, "mismatch": 0,
                         "recon_checked": 0, "recon_fixed": 0, "recon_samples": []}
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

        # 影子追蹤：記錄所有候選與對照組（學習資料），再決定是否真的往下產生實際訊號
        try:
            import shadow
            shadow.capture_candidates(all_signals, market_overview)
            shadow.flush_controls(list(self._ctrl))
            shadow.flush_rejects()
        except Exception as e:
            logger.warning(f"影子追蹤記錄失敗（不影響掃描）: {e}")
        if skip_new_signals:
            all_signals = []

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
        # ★ 修正：2026-09-27——使用者回報系統顯示「已有14個持倉，暫停新增（上限5）」，
        # 查證後發現這不是 _resolve_pending_signals() 逾期平倉邏輯失效（14筆訊號查
        # 出來全部只有3天，遠低於 signal_expire_days=15，本來就還不該被強制平倉），
        # 而是這裡的一個真正的bug：check_max_positions() 熔斷只在「整次掃描開始前」
        # 檢查一次未平倉數是否已經 >= MAX_SIMULTANEOUS_POSITIONS（見上方 Step 2/5
        # 之前的檢查），如果開始時数字還沒到上限（例如剩4個名額），熔斷不會觸發，
        # 掃描就會照常進行到這裡——但這裡原本只用 TELEGRAM_CONFIG["max_signals_
        # per_day"]（10）去截斷，跟 MAX_SIMULTANEOUS_POSITIONS（5）完全是兩個不
        # 相關的數字，等於「這次掃描最多推播幾則」跟「總共最多能有幾個未平倉部位」
        # 之間沒有任何關聯。2026-09-24 那次掃描開始時未平倉數還沒到5，但當天篩選後
        # 找到的候選一路推到 max_signals_per_day=10 這個上限，一次性把未平倉數從個位數
        # 推高到14，之後熔斷才在「已經超標」的狀態下持續擋住後續所有掃描——換句話說，
        # 熔斷本身沒壞，是「單次掃描可以新增幾筆」原本就沒有真正跟持倉上限掛勾。這裡
        # 補上第二層真正的上限：這次最多只能再新增 (MAX_SIMULTANEOUS_POSITIONS -
        # 目前未平倉數) 筆，兩個上限取較小值，之後不管單次掃描候選再多，未平倉數永遠
        # 不會超過 MAX_SIMULTANEOUS_POSITIONS。
        from config import MAX_SIMULTANEOUS_POSITIONS
        remaining_slots = max(0, MAX_SIMULTANEOUS_POSITIONS - len(active_positions))
        signal_cap = min(TELEGRAM_CONFIG["max_signals_per_day"], remaining_slots)
        # ★ 新增：2026-10-05——弱勢市場降載。9/30、10/1 當時市場情緒分數僅 32（偏空）、
        # 外資連續賣超，系統卻在兩天內連推 6 檔做多，且全部同方向、同時進場，等於把
        # 同一個「大盤再下殺」風險重複押了 6 次。情緒 < 40 時單次最多 2 檔，< 50 最多 3
        # 檔，把部位曝險隨大盤環境往下收，而不是只靠訊號分數（分數貼頂、沒有鑑別力）。
        try:
            _sent = float(market_overview.get("sentiment_score", 50) or 50)
        except Exception:
            _sent = 50
        _regime_cap = 2 if _sent < 40 else 3 if _sent < 50 else signal_cap
        if _regime_cap < signal_cap:
            logger.info(f"run_daily_scan: 市場情緒 {_sent:.0f}（偏弱），單次訊號上限由 {signal_cap} 降為 {_regime_cap}")
            signal_cap = _regime_cap
        final_signals = filtered[:signal_cap]
        if len(filtered) > signal_cap:
            logger.info(
                f"run_daily_scan: 篩選後 {len(filtered)} 檔候選，但持倉上限只剩 "
                f"{remaining_slots} 個名額（{len(active_positions)}/{MAX_SIMULTANEOUS_POSITIONS} 已佔用），"
                f"本次只取分數最高的 {len(final_signals)} 檔，其餘留到下次未平倉數降低後再掃"
            )

        try:
            import shadow
            shadow.mark_sent([x.get("ticker") for x in final_signals])
        except Exception as e:
            logger.warning(f"shadow.mark_sent: {e}")
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
            # 網頁通知中心：與 TG 推播同步記錄（即使 TG 推播失敗/被去重擋掉，網頁也看得到）
            try:
                store.add_event(
                    "signal",
                    f"{sig.get('name','')}（{sig.get('code','')}）{'做多' if sig.get('direction')=='buy' else '做空'}訊號 {sig.get('score')} 分",
                    f"進場 {sig.get('entry_price')}｜停損 {sig.get('stop_loss')}（{sig.get('sl_pct')}%）｜TP1 {sig.get('tp1')}｜追高風險 {({'low':'低','mid':'中','high':'高'}).get(sig.get('chase_level'), '—')}",
                    sig.get("ticker", ""),
                )
            except Exception as e:
                logger.warning(f"寫入網頁通知事件失敗（不影響訊號）: {e}")

        stats = {
            "scanned":       scanned_count,
            "errors":        error_count,
            "signals_found": len(all_signals),
            "signals_sent":  len(final_signals),
            "duration_min":  round((time.time() - start_time) / 60, 1),
            "duration_sec":  round(time.time() - start_time, 1),
        }
        store.save_scan_history(stats)
        try:
            pre = dict(self._pre)
            tot = pre["skipped"] + pre["passed"]
            store.set_meta("last_scan_audit", {
                "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
                "universe": total_tickers, "examined": tot, "skipped": pre["skipped"], "passed": pre["passed"],
                "skip_rate": round(pre["skipped"] / tot * 100, 1) if tot else None,
                "audited": pre["audited"], "mismatch": pre["mismatch"],
                "recon_checked": pre["recon_checked"], "recon_fixed": pre["recon_fixed"],
                "recon_samples": pre["recon_samples"], "duration_sec": stats["duration_sec"],
            })
        except Exception as e:
            logger.warning(f"寫入掃描稽核失敗: {e}")

        # 5. 推播
        logger.info("Step 5/5: 推播訊號...")
        self._push_signals(final_signals, market_overview, stats)

        try:
            import shadow
            shadow.resolve_pending()
            _p = dict(self._pre)
            shadow.finish_run(universe=total_tickers, pre_skipped=_p.get("skipped"), pre_passed=_p.get("passed"))
        except Exception as e:
            logger.warning(f"影子追蹤結算失敗（不影響掃描）: {e}")

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
                # 2026-10-07：訊號是收盤後才產生，實際最早只能隔天買進。損益改以「訊號日後第一根 K 棒的開盤價」為進場價
                # （原本用訊號日收盤價，等於假設能用收盤價成交，績效偏樂觀）。找不到開盤價才退回訊號價。
                sim_entry = None
                try:
                    _opens = data.get("opens") or []
                    for _i, _d in enumerate(dates):
                        if _d and _d > gen_date:
                            sim_entry = _opens[_i] if _i < len(_opens) and _opens[_i] else None
                            break
                except Exception:
                    sim_entry = None
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
                    if hit_sl:
                        result, close_price = "sl", sl
                        # 跳空穿越停損：實際只能以開盤價成交（較差者），不再樂觀地填在停損價
                        try:
                            op = (data.get("opens") or [None] * len(dates))[i]
                            if op:
                                if direction == "buy" and op < sl: close_price = op
                                elif direction == "sell" and op > sl: close_price = op
                        except Exception:
                            pass
                    elif hit_tp3: result, close_price = "tp3", tp3
                    elif hit_tp2: result, close_price = "tp2", tp2
                    else:         result, close_price = "tp1", tp1
                    entry  = sim_entry or sig.get("entry_price") or sig.get("current_price") or close_price
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
                        _rz = {"sl": "停損", "tp1": "停利一", "tp2": "停利二", "tp3": "停利三"}.get(result, result)
                        store.add_event("result", f"{sig.get('name','')}（{sig.get('code','')}）{_rz}",
                                        f"成交 {close_price:.2f}｜損益 {pnl:+,.0f} 元（{pnl_pct:+.1f}%）", ticker)
                    except Exception:
                        pass
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
                        entry  = sim_entry or sig.get("entry_price") or sig.get("current_price") or last_close
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
        import realtime
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._intraday_alerted.get("date") != today:
            self._intraday_alerted = {"date": today, "ids": set()}
        pending = store.get_pending_signals()
        if not pending:
            logger.info("check_intraday_price_alerts: 目前無未平倉訊號，跳過本次盤中檢查")
            return
        tickers = list({s["ticker"] for s in pending if s.get("ticker")})
        # 2026-10-07：現價只用官方即時（MIS，備援富果）且資料日期必須是今天；抓不到/過期就不判斷（fail closed），
        # 不再退回 Yahoo（延遲且是還原價，拿來比停損會誤報或漏報）。
        try:
            pairs = [(t, "OTC" if t.upper().endswith(".TWO") else None) for t in tickers]
            fp = realtime.fresh_prices(pairs)
            by_code = fp["prices"]
            prices = {t: by_code[t.split(".")[0].upper()] for t in tickers if t.split(".")[0].upper() in by_code}
            if fp["reasons"]:
                logger.info(f"check_intraday_price_alerts: 未納入比對 {fp['reasons']}")
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
            try:  # 跨實例／重啟去重：同一訊號同一種到價警示，一天只發一次
                from state_store import store as _st
                if not _st.claim_notice(f"intra:{sig_id}:{'sl' if hit_sl else 'tp'}:{today}", 86400):
                    continue
            except Exception:
                pass
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
    _pre_lock = threading.Lock()
    _pre = {"skipped": 0, "passed": 0, "audited": 0, "mismatch": 0, "recon_checked": 0, "recon_fixed": 0, "recon_samples": []}
    _ctrl = []
    _PRE_AUDIT_RATE = 0.05   # 被日線預判跳過的股票，隨機抽 5% 仍跑完整流程自我驗證
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

    def _reconcile_last_bar(self, ticker, stock_info, daily):
        """最新一根日K的收盤價，和證交所／櫃買中心官方收盤資料（股票池來源）逐檔核對。
        只在「兩邊是同一個交易日」時比對；差異超過 0.3% 以官方收盤價為準（進出場價位以官方為準），
        並記錄筆數與樣本。成交量不覆寫：Yahoo 的量與官方有系統性差異，但歷史均量也是同一來源，
        只換最後一根會讓量比失真，差異改在「資料稽核」頁揭露。"""
        try:
            qd = stock_info.get("quote_date")
            oc = stock_info.get("close")
            dates = daily.get("dates") or []
            if not (qd and oc and dates and dates[-1] == qd):
                return daily
            with self._pre_lock:
                self._pre["recon_checked"] += 1
            cur = daily["closes"][-1]
            if oc > 0 and abs(cur - oc) / oc > 0.003:
                d2 = dict(daily)
                d2["closes"] = list(daily["closes"]); d2["closes"][-1] = oc
                d2["current_price"] = oc
                prev = d2["closes"][-2] if len(d2["closes"]) > 1 else oc
                d2["change_pct"] = round((oc - prev) / prev * 100, 2) if prev else 0
                with self._pre_lock:
                    self._pre["recon_fixed"] += 1
                    if len(self._pre["recon_samples"]) < 8:
                        self._pre["recon_samples"].append({"ticker": ticker, "yahoo": cur, "official": oc, "date": qd})
                logger.warning(f"[RECON] {ticker} {qd} Yahoo收盤 {cur} ≠ 官方 {oc}，改用官方")
                return d2
        except Exception as e:
            logger.warning(f"_reconcile_last_bar {ticker}: {e}")
        return daily

    def _scan_single(self, ticker, market_overview, fetch_tf, fetch_inst, gen_signal, get_info) -> Optional[Dict]:
        stock_info = get_info(ticker)
        if not stock_info: return None
        # ★ 掃描加速（無損，且持續自我驗證）：先只抓日線做預判。週線只會「扣分或確認」、
        # 小時線最多只「加 10 分」，所以日線單獨算出 direction=none，最終一定是 none；
        # 日線分數 +10 仍低於門檻，也一定過不了。符合這兩種的股票不再重複抓週線／小時線。
        # 為了不讓這個推論只靠「我說它無損」，每次掃描都對被跳過的股票隨機抽 5% 仍跑完整流程，
        # 若抽驗到任何一檔其實會成為訊號，記為 mismatch 並在日誌與網頁「資料稽核」頁顯示。
        from data_fetcher import fetch_ohlcv
        from signal_engine import check_multi_timeframe_tw
        import random
        daily = fetch_ohlcv(ticker, "daily")
        if not daily: return None
        daily = self._reconcile_last_bar(ticker, stock_info, daily)
        pre = check_multi_timeframe_tw({"daily": daily})
        try:
            import shadow as _shd
            _shd.tally(stock_info, daily)
        except Exception:
            pass
        if pre.get("direction") == "none" or pre.get("score", 0) + 10 < THRESH["min_score"]:
            with self._pre_lock:
                self._pre["skipped"] += 1
            try:
                import shadow
                shadow.maybe_control(ticker, stock_info, daily, pre, market_overview, self._ctrl, self._pre_lock)
            except Exception:
                pass
            if random.random() < self._PRE_AUDIT_RATE:
                full_tf = fetch_tf(ticker)
                if full_tf:
                    full_tf["daily"] = daily
                    code_ = stock_info.get("code", ticker.replace(".TW","").replace(".TWO",""))
                    sig_ = gen_signal(ticker, stock_info, full_tf, market_overview, fetch_inst(code_))
                    with self._pre_lock:
                        self._pre["audited"] += 1
                        if sig_:
                            self._pre["mismatch"] += 1
                            logger.error(f"[PREFILTER-AUDIT] {ticker} 被日線預判跳過，但完整流程會產生訊號！")
            return None
        with self._pre_lock:
            self._pre["passed"] += 1
        tf_data = fetch_tf(ticker)
        if not tf_data: return None
        tf_data["daily"] = daily
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
        elif status == "data_error":
            # ★ 修正：2026-09-26（稽核 finding #2）——大盤指數資料抓不到時的新狀態，
            # 見 data_fetcher.fetch_market_index() 的說明。can_trade 已經在
            # fetch_market_overview() 被設成 False，signal_engine.generate_signal_tw()
            # 會直接因此擋掉所有新訊號（見該函式 can_trade 檢查），這裡只需要留下
            # 明確的 log，方便事後排查「今天為什麼完全沒訊號」時一眼看出是資料問題
            # 而不是策略本身沒找到機會。
            logger.warning(f"大盤指數資料異常（twii.source=='error'），本次暫停所有新倉")
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
        # ★ 修正：2026-09-26（稽核 finding #7，文件化澄清，邏輯不變）——這裡寫的是
        # 「大盤重挫時只留做空訊號」，但 config.py 的 ENABLE_SHORT_SIGNALS 目前是
        # False，signal_engine.check_multi_timeframe_tw() 在這個開關關閉時，一開始
        # 就不會產生 direction=="sell" 的訊號（見該檔案該函式），所以這一行實際
        # 的效果是「大盤重挫時清空所有訊號」，不是字面上看起來的「只做空」。這是
        # 目前正確、預期中的保守行為（大盤重挫时暫停一切新倉），保留這行是為了
        # ENABLE_SHORT_SIGNALS 未來重新開啟時，這裡不用再改一次就能自動變回
        # 「只留空單」的原始設計意圖；只是這裡留言澄清，避免日後誤以為這是漏掉
        # 開發、忘記寫多單過濾的bug。
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
            data_missing_count = 0
            for sig in signals:
                chk = check_fundamental_hard_filter(sig.get("code", ""), revenue_map)
                if chk["blocked"]:
                    fundamentally_blocked.append(f"{sig.get('ticker')}（{chk['reason']}）")
                else:
                    # ★ 新增：2026-09-28——見 fundamentals.py check_fundamental_hard_filter()
                    # 的說明：ChatGPT/Perplexity 都把「fail-open 時無法分辨『查過沒事』
                    # 跟『根本沒查到』」列為高優先風險。這裡把 data_missing 明確標在訊號
                    # 上（推播文字＋一個獨立欄位），讓使用者自己判斷要不要對這種訊號
                    # 更保守，而不是讓它看起來跟「已通過月營收檢查」的訊號一模一樣。
                    sig["revenue_check"] = "data_missing" if chk.get("data_missing") else "passed"
                    if chk.get("data_missing"):
                        data_missing_count += 1
                        sig["reason_full"] = sig.get("reason_full", "") + \
                            "\n⚠️【月營收】資料無法取得，本檔本次未經過本業衰退檢查（非「已確認正常」）"
                    kept.append(sig)
            signals = kept
            if fundamentally_blocked:
                logger.info(f"_filter_and_rank: 基本面硬性過濾排除 {len(fundamentally_blocked)} 檔：{fundamentally_blocked}")
            if data_missing_count:
                logger.info(f"_filter_and_rank: {data_missing_count} 檔月營收資料缺失、未實際檢查（fail-open，已標記在訊號上）")
            logger.info(f"_filter_and_rank: 基本面過濾後剩 {len(signals)}/{before} 檔")
        except Exception as e:
            logger.warning(f"_filter_and_rank: 基本面過濾失敗（不影響本次掃描，本次跳過基本面過濾）: {e}")

        # ★ 新增：2026-09-27——使用者要求把「估值面(本益比/股價淨值比/殖利率) +
        # 融券餘額/當沖比例（籌碼面風險）」接進評分邏輯（見 fundamentals.py
        # fetch_valuation_map()/fetch_margin_short_map() 的資料擷取層說明）。
        # 設計決定（跟月營收「硬性排除」不同，比照法人買賣超「加減分」模式）：
        #   1) 融券餘額單日大幅增加（放空/避險力道增加，對 buy 訊號是反向證據）
        #      →扣分；融券使用率接近限額（可能被迫回補、空方回補買盤是 buy 的
        #      潛在助力）→加分。兩者都是「已經有明確方向性解釋」的訊號，比照
        #      inst_data 的±5分寫成對稱規則（sell 訊號目前因 ENABLE_SHORT_SIGNALS
        #      關閉不會被產生，但規則本身照樣寫成對稱，避免之後重新開放做空時
        #      又要重蹈 inst_data 那次不對稱的覆轍）。
        #   2) 估值面(PE/PB/殖利率) 跟大盤層級的當沖比重 overlay，目前只當「顯示
        #      資訊」附掛在訊號上（reason_full 多一行），不影響分數——這兩塊還
        #      沒有實測資料驗證過合理的加減分門檻/幅度，貿然訂一個數字去動分數
        #      風險比「先只顯示」更高（跟這個專案一路的保守原則一致：ENABLE_
        #      SHORT_SIGNALS 就是因為回測結果不好才關掉，不是先猜測再上線）。
        try:
            from fundamentals import fetch_margin_short_map, fetch_valuation_map, fetch_market_daytrading_overlay
            margin_short_map = fetch_margin_short_map()
            valuation_map = fetch_valuation_map()
            daytrading_overlay = fetch_market_daytrading_overlay()
            for sig in signals:
                code = sig.get("code", "")
                chip_adj = 0
                chip_notes = []
                ms = margin_short_map.get(code)
                if ms:
                    chg = ms.get("short_balance_chg")
                    prev = ms.get("short_balance") - chg if (chg is not None and ms.get("short_balance") is not None) else None
                    chg_pct = (chg / prev * 100) if (chg and prev and prev > 0) else None
                    util = ms.get("short_utilization_pct")
                    if sig["direction"] == "buy":
                        if chg_pct is not None and chg_pct > 30 and chg > 20:
                            chip_adj -= 3
                            chip_notes.append(f"融券餘額單日+{chg_pct:.0f}%（空方力道增加）⚠️")
                        elif chg_pct is not None and chg_pct < -30 and chg < -20:
                            chip_adj += 2
                            chip_notes.append(f"融券餘額單日{chg_pct:.0f}%（空方回補）")
                        if util is not None and util >= 90:
                            chip_adj += 2
                            chip_notes.append(f"融券使用率{util:.0f}%接近限額（潛在回補買盤）")
                    else:  # sell（目前 ENABLE_SHORT_SIGNALS=False 不會實際產生，規則對稱保留）
                        if chg_pct is not None and chg_pct < -30 and chg < -20:
                            chip_adj -= 3
                            chip_notes.append(f"融券餘額單日{chg_pct:.0f}%（空方回補，反向證據）⚠️")
                        elif chg_pct is not None and chg_pct > 30 and chg > 20:
                            chip_adj += 2
                            chip_notes.append(f"融券餘額單日+{chg_pct:.0f}%（空方力道增加）")
                if chip_adj:
                    sig["score"] = max(0, min(100, sig["score"] + chip_adj))
                    logger.info(f"[{sig.get('ticker')}] 籌碼面評分調整 {chip_adj:+d}：{'；'.join(chip_notes)}")
                val = valuation_map.get(code)
                chip_display_lines = []
                if chip_notes:
                    chip_display_lines.append("【籌碼】" + "；".join(chip_notes))
                if val and (val.get("pe") is not None or val.get("pb") is not None):
                    chip_display_lines.append(
                        f"【估值】PE={val.get('pe','—')} PB={val.get('pb','—')} 殖利率={val.get('yield_pct','—')}%"
                    )
                if daytrading_overlay:
                    chip_display_lines.append(
                        f"【當沖(大盤櫃買)】成交值占比 買{daytrading_overlay.get('buy_value_pct_of_market','—')}% "
                        f"/ 賣{daytrading_overlay.get('sell_value_pct_of_market','—')}%"
                    )
                if chip_display_lines:
                    sig["reason_full"] = sig.get("reason_full", "") + "\n" + "\n".join(chip_display_lines)
                    sig["chip_risk_note"] = "；".join(chip_notes) if chip_notes else ""
            before = len(signals)
            signals = [s for s in signals if s["score"] >= THRESH["min_score"]]
            if len(signals) != before:
                logger.info(f"_filter_and_rank: 籌碼面評分調整後，{before - len(signals)} 檔跌破門檻被排除，剩 {len(signals)} 檔")
        except Exception as e:
            logger.warning(f"_filter_and_rank: 籌碼面評分調整失敗（不影響本次掃描，本次跳過）: {e}")

        # ★ 新增：2026-09-28——使用者要求把「獲利品質（毛利率/營業利益率
        # 趨勢）」跟「重大訊息公告（紅旗關鍵字）」接進評分邏輯。設計決定
        # 跟上面融券餘額/融券使用率一樣走「加減分」模式，不做硬性排除：
        #   1) 獲利品質目前涵蓋上市「一般業/保險業/證券期貨業/金控業/銀行業」
        #      五個子分類（見 fundamentals.py fetch_profitability_quality_map()
        #      說明），異業分類（mim）跟全部上櫃公司還沒有，覆蓋率不到全市場，
        #      硬性排除會讓沒資料的標的處於「永遠不會被擋」的不公平狀態，
        #      跟月營收硬性過濾「找不到資料一律不擋」的公平性原則矛盾；用
        #      加減分則沒資料時本來就是「不加不減」，沒有這個公平性問題。
        #   2) 重大訊息公告是關鍵字比對，不是語意判斷，一定有誤判機率，
        #      比照法人買賣超/籌碼面的「小幅加減分」而非「一票否決」，
        #      把誤判的下行風險限制在可接受範圍內。
        try:
            # ★ 修正：2026-09-28——這裡刻意用 get_profitability_quality(code)
            # 逐檔查（只對「這次候選訊號」的幾十檔個股做資料庫讀寫），不是
            # fetch_profitability_quality_map() 整包全市場的 map——後者上線
            # 後實測對全市場900+檔都做DB讀寫，把 /api/state 這種既有端點都
            # 拖到逾時，已修正（見 fundamentals.py 該函式的說明）。
            from fundamentals import (
                get_profitability_quality, fetch_material_news_risk_map,
                material_news_fetch_ok, profitability_quality_fetch_ok,
            )
            news_risk_map = fetch_material_news_risk_map()
            # ★ 新增：2026-09-28——item 2（fail-open 三態化，ChatGPT/Perplexity
            # 第二輪都列為最優先項目）：「沒查到」跟「查過確認乾淨」原本在
            # sig 上長得一模一樣，使用者只能靠「加減分沒動」猜。這裡比照
            # revenue_check 的模式，明確標三態：
            #   checked      = 這次真的查過這檔（不管結果是命中/沒命中）
            #   not_covered  = 這檔本來就不在資料源涵蓋範圍（上櫃／異業／
            #                  重大訊息只做上市），不是抓取失敗
            #   unavailable  = 資料源這次整體抓取失敗，fail-open 放行，但
            #                  「沒有風險提示」不代表「已確認乾淨」
            _news_ok = material_news_fetch_ok()
            _quality_ok = profitability_quality_fetch_ok()
            for sig in signals:
                code = sig.get("code", "")
                quality_adj = 0
                quality_notes = []
                q = get_profitability_quality(code)
                # revenue_map 的 source 欄位（twse_openapi/tpex_openapi）用來
                # 判斷這檔的市場別——重大訊息公告只有上市公司的資料源，上櫃
                # 一律 not_covered，不管這次 fetch 有沒有成功。
                _market_source = revenue_map.get(code, {}).get("source", "")
                if _market_source == "tpex_openapi":
                    sig["news_check"] = "not_covered"
                elif _news_ok:
                    sig["news_check"] = "checked"
                else:
                    sig["news_check"] = "unavailable"
                # 獲利品質目前只做上市「一般業/保險業/證券期貨業/金控業/
                # 銀行業」五個子分類，上櫃／異業(mim) 一律 not_covered；q 不是
                # None 就代表這次確實查到、算出結果了（checked）。
                if q is not None:
                    sig["quality_check"] = "checked"
                elif _market_source == "tpex_openapi":
                    sig["quality_check"] = "not_covered"
                elif not _quality_ok:
                    sig["quality_check"] = "unavailable"
                else:
                    # 上市但沒查到，且這次五個子端點至少有一個抓成功——
                    # 最可能是異業(mim)分類，目前還沒做，一律當 not_covered。
                    sig["quality_check"] = "not_covered"
                if sig["quality_check"] == "unavailable" or sig["news_check"] == "unavailable":
                    sig["reason_full"] = sig.get("reason_full", "") + (
                        "\n⚠️【資料品質】" +
                        ("獲利品質本次無法取得、未實際檢查；" if sig["quality_check"] == "unavailable" else "") +
                        ("重大訊息本次無法取得、未實際檢查；" if sig["news_check"] == "unavailable" else "") +
                        "非「已確認正常」"
                    )
                if q:
                    gm_chg = q.get("gross_margin_chg")
                    om_chg = q.get("operating_margin_chg")
                    # ★ 修正：2026-09-28——優先用反推出來的「單季」營業利益率
                    # (operating_margin_pct_single_q)，不是累計數的
                    # operating_margin_pct（見 fundamentals.py get_profitability_
                    # quality() 說明：累計數的比率在Q2以後會被之前幾季稀釋，不是
                    # 真正的「本季」水準）。只有第一次看到這檔股票、還沒有上一季
                    # 資料可反推時，single_q 會是 None，才退回累計數當近似值。
                    om = q.get("operating_margin_pct_single_q")
                    if om is None:
                        om = q.get("operating_margin_pct")
                    if sig["direction"] == "buy":
                        if om is not None and om < 0:
                            quality_adj -= 3
                            quality_notes.append(f"本季營業利益率{om:.1f}%（虧損）⚠️")
                        if gm_chg is not None and gm_chg <= -3:
                            quality_adj -= 2
                            quality_notes.append(f"毛利率季減{abs(gm_chg):.1f}個百分點")
                        elif gm_chg is not None and gm_chg >= 3:
                            quality_adj += 1
                            quality_notes.append(f"毛利率季增{gm_chg:.1f}個百分點")
                        if om_chg is not None and om_chg <= -3:
                            quality_adj -= 2
                            quality_notes.append(f"營業利益率季減{abs(om_chg):.1f}個百分點")
                    else:  # sell（目前 ENABLE_SHORT_SIGNALS=False 不會實際產生，規則對稱保留）
                        if om is not None and om < 0:
                            quality_adj += 2
                            quality_notes.append(f"本季營業利益率{om:.1f}%（虧損，放空方向支持）")
                        if gm_chg is not None and gm_chg <= -3:
                            quality_adj += 1
                            quality_notes.append(f"毛利率季減{abs(gm_chg):.1f}個百分點（放空方向支持）")
                news_hits = news_risk_map.get(code)
                if news_hits and sig["direction"] == "buy":
                    quality_adj -= 4
                    quality_notes.append(f"近期重大訊息：{news_hits[0].get('subject','')}⚠️")
                if quality_adj:
                    sig["score"] = max(0, min(100, sig["score"] + quality_adj))
                    logger.info(f"[{sig.get('ticker')}] 獲利品質/重大訊息評分調整 {quality_adj:+d}：{'；'.join(quality_notes)}")
                display_lines = []
                if q and (q.get("gross_margin_pct") is not None or q.get("operating_margin_pct") is not None):
                    gm_disp = q.get("gross_margin_pct_single_q")
                    om_disp = q.get("operating_margin_pct_single_q")
                    if gm_disp is not None or om_disp is not None:
                        display_lines.append(
                            f"【獲利品質】單季毛利率={gm_disp if gm_disp is not None else '—'}% "
                            f"單季營業利益率={om_disp if om_disp is not None else '—'}%（{q.get('period','')}）"
                        )
                    else:
                        display_lines.append(
                            f"【獲利品質】累計毛利率={q.get('gross_margin_pct','—')}% "
                            f"累計營業利益率={q.get('operating_margin_pct','—')}%（{q.get('period','')}，"
                            f"尚無上一季資料可反推單季數字）"
                        )
                if news_hits:
                    display_lines.append("【重大訊息】" + "；".join(h.get("subject", "") for h in news_hits[:2]))
                if display_lines:
                    sig["reason_full"] = sig.get("reason_full", "") + "\n" + "\n".join(display_lines)
            before = len(signals)
            signals = [s for s in signals if s["score"] >= THRESH["min_score"]]
            if len(signals) != before:
                logger.info(f"_filter_and_rank: 獲利品質/重大訊息評分調整後，{before - len(signals)} 檔跌破門檻被排除，剩 {len(signals)} 檔")
        except Exception as e:
            logger.warning(f"_filter_and_rank: 獲利品質/重大訊息評分調整失敗（不影響本次掃描，本次跳過）: {e}")

        # ★ 新增：2026-10-05——基本面評分（月營收單月/累計/加速度＋獲利品質趨勢＋估值）。
        # 使用者要求策略不能只靠 K 線：技術面決定「何時」，基本面決定「值不值得」。
        # 做多訊號依基本面分數加減分（強 +5～弱 -12），太差的自然跌破門檻被排除；
        # 資料涵蓋不足（<40%）時不調分，並在訊號上標示 fund_grade=None。
        try:
            from fund_score import build_fundamental_profile, fund_adjustment
            from fund_score import _persist_current_period
            from fundamentals import fetch_monthly_revenue_map as _frm
            _persist_current_period(_frm())
            before = len(signals)
            for sig in signals:
                prof = build_fundamental_profile(sig.get("code", ""))
                adj = fund_adjustment(prof, sig["direction"])
                sig["fund_score"] = prof.get("score")
                sig["fund_grade"] = prof.get("grade")
                sig["fund_coverage"] = prof.get("coverage")
                sig["fund_tags"] = prof.get("tags", [])[:5]
                sig["fund_adj"] = adj
                if adj:
                    sig["score"] = max(0, min(100, sig["score"] + adj))
                if prof.get("score") is not None:
                    sig["reason_full"] = sig.get("reason_full", "") + (
                        f"\n【基本面】{prof['grade']}（{prof['score']}分，涵蓋{prof['coverage']}%）"
                        + ("：" + "；".join(prof["tags"][:4]) if prof.get("tags") else "")
                        + (f"｜評分{adj:+d}" if adj else ""))
                else:
                    sig["reason_full"] = sig.get("reason_full", "") + "\n【基本面】資料不足，本檔未納入基本面評分"
            signals = [s for s in signals if s["score"] >= THRESH["min_score"]]
            if len(signals) != before:
                logger.info(f"_filter_and_rank: 基本面評分調整後，{before - len(signals)} 檔跌破門檻被排除，剩 {len(signals)} 檔")
        except Exception as e:
            logger.warning(f"_filter_and_rank: 基本面評分失敗（不影響本次掃描，本次跳過）: {e}")

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
                if key not in sector_best or (sig["score"] - sig.get("chase_pen", 0)) > (sector_best[key]["score"] - sector_best[key].get("chase_pen", 0)):
                    sector_best[key] = sig
            combined = list(sector_best.values())
        combined.sort(key=lambda x: x["score"] - x.get("chase_pen", 0), reverse=True)
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

        # ★ 修正：2026-09-29——使用者回報 Telegram「一直亂跳訊息」，實際是同一份
        # 「盤後集結報告」跟風控熔斷警示在同一天被完整重複推播了兩次（15:39跟
        # 16:30 各一次，兩次的VIX/外資數字有些微差異，代表是兩次真正各自獨立
        # 的完整掃描，不是同一次送兩份）。run_daily_scan() 本身只用鎖擋「同時」
        # 執行兩次，完全沒有擋「同一天」執行第二次——不管第二次是使用者自己
        # 手動按了「立即掃描」、還是 16:15 job_pre_scan_restart 重啟 worker 後
        # 舊/新兩個 worker 短暫並存導致 16:30 的排程意外跑了兩次，只要當天
        # 已經有一次完整報告送出去，同一天再送一次一定是重複雜訊，不會是使用者
        # 想看到的。這裡在真正推播「總結報告」前用 state_store 記一個「今天
        # 是否已經送過」的旗標（跨 process/跨重啟都看得到，不是只存在記憶體），
        # 已經送過就跳過本次總結報告（＋後面的個別訊號推播，因為那些訊號本來
        # 就是同一次完整掃描找出來的，這次掃描如果整個是「重複」，個別訊號也
        # 沒有必要重推一次），只在 log 留一筆記錄方便日後排查，而不是靜靜吞掉
        # 讓機主一頭霧水查不到原因。
        try:
            from state_store import store
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            already_sent_date = store.get_meta("daily_report_sent_date", "")
            if already_sent_date == today_str:
                logger.warning(f"_push_signals: 今天（{today_str}）已經送過一次盤後集結報告，"
                                f"本次視為重複觸發（手動立即掃描或排程/重啟重疊），跳過推播避免 Telegram 重複洗版")
                return
        except Exception as e:
            logger.warning(f"_push_signals: 讀取今日是否已推播的旗標失敗（{e}），保守起見繼續照常推播")
            today_str = None

        try:
            send_daily_report(signals, market_overview, stats)
            if today_str:
                try:
                    store.set_meta("daily_report_sent_date", today_str)
                except Exception as e:
                    logger.warning(f"_push_signals: 寫入今日已推播旗標失敗（不影響本次已送出的報告）: {e}")
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
