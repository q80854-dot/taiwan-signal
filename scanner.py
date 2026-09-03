"""
scanner.py — 全市場掃描引擎 v1.0
每日 16:30 盤後觸發，批次掃描 1000+ 檔台股
"""
import time, logging, threading
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
        from data_fetcher   import fetch_all_timeframes, fetch_market_overview, fetch_stock_institutional
        from signal_engine  import generate_signal_tw
        from state_store    import store

        # 0. 結算舊訊號（見 _resolve_pending_signals 說明）
        logger.info("Step 0/5: 結算舊訊號的停損/停利...")
        try:
            self._resolve_pending_signals()
        except Exception as e:
            logger.error(f"_resolve_pending_signals: {e}", exc_info=True)

        # 1. 市場總覽
        logger.info("Step 1/5: 抓取市場環境...")
        market_overview = fetch_market_overview()
        self._check_market_status(market_overview)

        # 2. 品種清單
        logger.info("Step 2/5: 建立掃描清單...")
        batches       = get_scan_batches(batch_size=SYSTEM["scan_batch_size"])
        total_tickers = sum(len(b) for b in batches)
        logger.info(f"共 {total_tickers} 檔，分 {len(batches)} 批")

        # 3. 批次掃描
        logger.info("Step 3/5: 開始批次掃描...")
        all_signals   = []
        scanned_count = 0
        error_count   = 0

        for batch_idx, batch in enumerate(batches):
            logger.info(f"批次 {batch_idx+1}/{len(batches)}（{len(batch)} 檔）")
            for ticker in batch:
                try:
                    sig = self._scan_single(ticker, market_overview,
                                            fetch_all_timeframes, fetch_stock_institutional,
                                            generate_signal_tw, get_stock_info)
                    if sig:
                        all_signals.append(sig)
                    scanned_count += 1
                except Exception as e:
                    error_count += 1
                    logger.warning(f"[{ticker}] 掃描錯誤: {e}")
                time.sleep(SYSTEM["scan_delay_sec"])
            if batch_idx < len(batches) - 1:
                time.sleep(3)

        # 4. 過濾排序
        logger.info("Step 4/5: 過濾與排序...")
        filtered = self._filter_and_rank(all_signals, market_overview)
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
                data = fetch_ohlcv(ticker, "daily")
                if not data:
                    continue
                dates, highs, lows = data.get("dates", []), data.get("highs", []), data.get("lows", [])
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
            except Exception as e:
                logger.warning(f"_resolve_pending_signals {sig.get('id')}: {e}")
        if resolved:
            logger.info(f"平倉結算：{resolved} 筆訊號已達停損/停利")

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

    def _filter_and_rank(self, signals: List[Dict], market_overview: Dict) -> List[Dict]:
        if not signals: return []
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
        try:
            from telegram_bot import push_signal, send_daily_report
            send_daily_report(signals, market_overview, stats)
            time.sleep(1)
            for sig in signals:
                push_signal(sig)
                time.sleep(0.5)
        except Exception as e:
            logger.error(f"_push_signals: {e}")

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
