"""
scanner.py — 全市場掃描引擎 v1.0
每日 16:30 盤後觸發，批次掃描 1000+ 檔台股
"""
import time, logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

from config import SIGNAL_THRESHOLDS as THRESH, CIRCUIT_BREAKER as CB, SYSTEM, TELEGRAM_CONFIG


class TWScanEngine:

    def __init__(self):
        self.signals_today: List[Dict] = []
        self.scan_count:    int        = 0
        self.last_scan_at:  str        = ""
        self.scan_errors:   List[str]  = []

    def run_daily_scan(self) -> Dict:
        start_time = time.time()
        logger.info("═══ 開始每日全市場掃描 ═══")

        from stock_universe import get_scan_batches, get_stock_info
        from data_fetcher   import fetch_all_timeframes, fetch_market_overview, fetch_stock_institutional
        from signal_engine  import generate_signal_tw
        from state_store    import store

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
        if status == "stop":
            logger.warning(f"大盤重挫 {twii_chg:.1f}%，本次只掃空單")
        elif status == "caution":
            logger.warning(f"大盤偏弱 {twii_chg:.1f}%，提高門檻")
            THRESH["min_score"] = min(75, THRESH["min_score"] + 5)

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
        }


scanner = TWScanEngine()
