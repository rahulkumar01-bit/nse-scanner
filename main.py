"""
Entry point. Runs a scan loop during NSE market hours, screening the
configured universe and emailing alerts for anything that crosses the
signal threshold.

Usage:
    python main.py            # continuous loop, market hours only
    python main.py --once     # single scan, useful for testing / cron
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime

import pytz

import config
import data_fetcher
import notifier
import screener
import state_store
from kotak_client import KotakClient

os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)

_IST = pytz.timezone(config.TIMEZONE)


class ISTFormatter(logging.Formatter):
    """Renders log timestamps in IST regardless of the host machine's
    local timezone (GitHub Actions runners default to UTC)."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S IST")


_formatter = ISTFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_file_handler = logging.FileHandler(config.LOG_FILE)
_file_handler.setFormatter(_formatter)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger("nse_scanner.main")


def is_market_open(now=None):
    tz = pytz.timezone(config.TIMEZONE)
    now = now or datetime.now(tz)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = datetime.strptime(config.MARKET_OPEN, "%H:%M").time()
    close_t = datetime.strptime(config.MARKET_CLOSE, "%H:%M").time()
    return open_t <= now.time() <= close_t


def run_scan(kc, token_map, universe, yf_baseline):
    alerts = []
    for symbol in universe:
        try:
            baseline = yf_baseline.get(symbol)
            if not baseline:
                continue  # no Yahoo Finance baseline available for this symbol — skip it

            if config.SCAN_EQUITIES:
                token = token_map.get(symbol)
                if token:
                    live = data_fetcher.fetch_live_quote(kc, token, exchange_segment="nse_cm")
                    result = screener.evaluate(symbol, baseline, live, instrument="EQ")
                    if result and state_store.should_alert(symbol, "EQ"):
                        alerts.append(result)
                        state_store.mark_alerted(symbol, "EQ")

            if config.SCAN_FNO:
                # Scans the current + next few monthly expiries (config.FNO_MAX_EXPIRIES),
                # not just the front month, since you're interested in later months too.
                for expiry_label, fut_token in data_fetcher.get_fno_tokens(kc, symbol):
                    instrument_label = f"FUT-{expiry_label}"
                    live_fut = data_fetcher.fetch_live_quote(kc, fut_token, exchange_segment="nse_fo")
                    oi_change_pct = None
                    if live_fut and live_fut.get("oi") is not None:
                        oi_key = f"{symbol}:{expiry_label}"
                        prev_oi = state_store.get_previous_oi(oi_key)
                        if prev_oi:
                            oi_change_pct = (live_fut["oi"] - prev_oi) / prev_oi * 100
                        state_store.record_oi(oi_key, live_fut["oi"])
                    # Futures technical baseline is approximated from the underlying's
                    # cash-market history — Kotak doesn't expose historical futures
                    # candles either, and stock futures track the underlying closely.
                    result = screener.evaluate(symbol, baseline, live_fut, instrument=instrument_label,
                                                oi_change_pct=oi_change_pct)
                    if result and state_store.should_alert(symbol, instrument_label):
                        alerts.append(result)
                        state_store.mark_alerted(symbol, instrument_label)

        except Exception:
            log.exception("Error screening %s", symbol)

    if alerts:
        log.info("Found %d signal(s): %s", len(alerts), [a["symbol"] for a in alerts])
        notifier.send_alerts(alerts)
    else:
        log.info("Scan complete — no signals this cycle")
        if not state_store.has_sent_heartbeat():
            if notifier.send_heartbeat():
                state_store.mark_heartbeat_sent()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    args = parser.parse_args()

    if args.once and not is_market_open():
        log.info("Market closed — skipping this run (invoked outside 09:15-15:30 IST, Mon-Fri)")
        return

    universe = data_fetcher.load_universe()
    log.info("Loaded universe of %d symbols", len(universe))

    kc = KotakClient()
    kc.login()
    token_map = data_fetcher.build_token_map(kc, universe)
    log.info("Resolved %d/%d symbols to instrument tokens", len(token_map), len(universe))

    yf_baseline = data_fetcher.fetch_yf_baseline_batch(universe)

    if args.once:
        run_scan(kc, token_map, universe, yf_baseline)
        return

    log.info("Starting continuous scan loop (every %d min, %s-%s %s)",
              config.SCAN_INTERVAL_MINUTES, config.MARKET_OPEN, config.MARKET_CLOSE, config.TIMEZONE)
    while True:
        if is_market_open():
            kc.ensure_session()
            # Cheap no-op most of the day (cache hit) — only actually re-fetches
            # from Yahoo Finance once, right after the calendar date rolls over.
            yf_baseline = data_fetcher.fetch_yf_baseline_batch(universe)
            run_scan(kc, token_map, universe, yf_baseline)
            time.sleep(config.SCAN_INTERVAL_MINUTES * 60)
        else:
            log.info("Market closed — sleeping 10 min")
            time.sleep(600)


if __name__ == "__main__":
    main()
