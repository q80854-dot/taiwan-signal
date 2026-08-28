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
