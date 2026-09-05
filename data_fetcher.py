"""
Two data sources, combined:

- Yahoo Finance (yfinance) supplies the historical daily OHLCV baseline
  (20-day avg volume, 20-day high, RSI-14, previous close) as of the last
  COMPLETED trading day. Kotak's API doesn't expose historical candle data
  (confirmed: their SDK's own feature list only lists live quotes, scrip
  master, and search — no historical/candle endpoint), so this fills that
  gap with a free source. Since this data doesn't change intraday, it's
  computed once per calendar day and cached to disk — the scanner runs
  every 10 minutes, but Yahoo only gets hit once a day per symbol.
- Kotak Neo supplies today's LIVE number (LTP, volume-so-far, OI for
  futures) via quotes(), refreshed every scan cycle.

The screener combines "yesterday's baseline" with "today's live snapshot"
for each check.
"""
import json
import logging
import os
import statistics
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz
import yfinance as yf

import config

log = logging.getLogger("nse_scanner.data_fetcher")

_IST = pytz.timezone(config.TIMEZONE)
YF_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "yf_cache.json")

# Yahoo Finance's anti-bot measures increasingly block plain requests from
# shared cloud IPs (GitHub Actions runners in particular) with cryptic
# "possibly delisted" errors on perfectly valid symbols. As of recent
# yfinance versions, having curl_cffi installed alongside yfinance is enough
# — yfinance auto-detects and uses it internally (with Chrome impersonation)
# for its own requests without any extra code here. requirements.txt pins
# curl_cffi for exactly this reason; nothing to wire up manually.


def load_universe():
    df = pd.read_csv(config.UNIVERSE_FILE)
    return [s.strip().upper() for s in df["symbol"].tolist()]


def _extract_rows(payload, _depth=0):
    """
    Several Kotak endpoints (search_scrip, quotes) should return a bare list
    per their docs, but in practice the response is sometimes wrapped in a
    dict (e.g. {"data": [...]} or {"message": {"data": [...]}}, or a single
    row without a list wrapper at all). Unwrap defensively, recursing one
    level into any nested dict, and treat a single scrip/quote-shaped dict
    as a one-row list.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "result", "results", "scrips", "list", "message",
                    "Success", "success"):
            if key not in payload:
                continue
            val = payload[key]
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                if any(k.startswith(("p", "l")) for k in val.keys()):
                    return [val]  # a single scrip/quote row (pSymbol, lExpiryDate, etc.)
                if _depth < 2:
                    nested = _extract_rows(val, _depth + 1)
                    if nested:
                        return nested
        if _depth == 0 and payload:
            message = payload.get("message")
            if isinstance(message, str) and "no data found" in message.lower():
                # A legitimate "no match for this search" response from Kotak,
                # not an error — happens for a handful of symbols whose search
                # terms don't resolve exactly. Not worth a warning every time.
                return []
            try:
                dumped = json.dumps(payload, default=str)[:1000]
            except Exception:
                dumped = repr(payload)[:1000]
            log.warning("Unrecognized dict response shape (full payload): %s", dumped)
    return []


def build_token_map(kc, symbols, exchange_segment="nse_cm"):
    """
    Resolves each symbol to its instrument token via search_scrip() (which
    is cached client-side per day by the SDK, so this doesn't hit the
    network hard even for a large universe). Returns {symbol: instrument_token}.
    """
    token_map = {}
    for symbol in symbols:
        try:
            raw = kc.search_scrip(exchange_segment=exchange_segment, symbol=symbol)
        except Exception:
            log.exception("search_scrip failed for %s", symbol)
            continue

        for row in _extract_rows(raw):
            if not isinstance(row, dict):
                continue
            trd_symbol = (row.get("pTrdSymbol") or "").upper()
            sym_name = (row.get("pSymbolName") or "").upper()
            group = (row.get("pGroup") or "").upper()
            token = row.get("pSymbol")
            if sym_name == symbol and token and (trd_symbol == f"{symbol}-EQ" or group == "EQ"):
                token_map[symbol] = token
                break
    return token_map


def get_fno_tokens(kc, underlying_symbol, max_expiries=None):
    """
    Resolves the N nearest unexpired monthly futures contracts for an
    underlying via search_scrip(..., option_type="FUT"). Returns a list of
    (expiry_label, instrument_token) tuples, nearest expiry first —
    e.g. [("25SEP", 51234), ("30OCT", 51298), ("27NOV", 51350)].
    """
    max_expiries = max_expiries or config.FNO_MAX_EXPIRIES
    try:
        raw = kc.search_scrip(exchange_segment="nse_fo", symbol=underlying_symbol, option_type="FUT")
    except Exception:
        log.exception("search_scrip (FUT) failed for %s", underlying_symbol)
        return []
    results = [r for r in _extract_rows(raw) if isinstance(r, dict)]
    if not results:
        return []

    def expiry_epoch(row):
        try:
            return float(row.get("lExpiryDate"))
        except (TypeError, ValueError):
            return float("inf")

    now_epoch = datetime.now().timestamp()
    future = [r for r in results if expiry_epoch(r) >= now_epoch]
    future.sort(key=expiry_epoch)

    out = []
    for row in future[:max_expiries]:
        token = row.get("pSymbol")
        if not token:
            continue
        try:
            label = datetime.fromtimestamp(expiry_epoch(row)).strftime("%d%b").upper()
        except (ValueError, OverflowError):
            label = "UNKNOWN"
        out.append((label, token))
    return out


def fetch_live_quote(kc, instrument_token, exchange_segment="nse_cm"):
    """
    Fetches today's live LTP / volume-so-far / OI from Kotak for a resolved
    instrument token. Response field names have varied across Kotak SDK
    versions, so several are tried per field.
    """
    try:
        raw = kc.quotes([{"instrument_token": str(instrument_token), "exchange_segment": exchange_segment}])
    except Exception:
        log.exception("quotes() failed for token %s", instrument_token)
        return None

    rows = _extract_rows(raw)
    if not rows and isinstance(raw, dict) and any(
            k in raw for k in ("last_traded_price", "ltp", "lastTradedPrice")):
        rows = [raw]
    if not rows or not isinstance(rows[0], dict):
        log.warning("quotes() returned an unrecognized shape for token %s: %r", instrument_token, raw)
        return None

    row = rows[0]
    ltp = row.get("last_traded_price") or row.get("ltp") or row.get("lastTradedPrice")
    volume = row.get("volume") or row.get("totalTradedQuantity") or row.get("vol")
    oi = row.get("open_interest") or row.get("oi") or row.get("openInterest")

    try:
        ltp = float(ltp)
    except (TypeError, ValueError):
        log.warning("quotes() missing a usable LTP for token %s: %r", instrument_token, row)
        return None
    try:
        volume = float(volume) if volume is not None else None
    except (TypeError, ValueError):
        volume = None
    try:
        oi = float(oi) if oi is not None else None
    except (TypeError, ValueError):
        oi = None

    return {"ltp": ltp, "volume": volume, "oi": oi}


def _today_ist_str():
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _load_yf_cache():
    if not os.path.exists(YF_CACHE_FILE):
        return {}
    try:
        with open(YF_CACHE_FILE) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if cache.get("date") != _today_ist_str():
        return {}
    return cache.get("data", {})


def _save_yf_cache(data):
    os.makedirs(os.path.dirname(YF_CACHE_FILE), exist_ok=True)
    with open(YF_CACHE_FILE, "w") as f:
        json.dump({"date": _today_ist_str(), "data": data}, f, indent=2)


def _rsi_series(close, period):
    """Wilder-style RSI (simple rolling-mean variant), as a pandas Series
    aligned to `close`'s index. Shared by the daily baseline calc and the
    long-history backtest so both use an identical definition of RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _atr_series(df, period):
    """Average True Range as a pandas Series. `df` needs high/low/close
    columns (lowercase). Shared by the daily baseline calc (ATR-14) and the
    long-history stats (ATR-252, a much less noisy read on a stock's
    "normal" volatility than a 14-day window taken mid-breakout)."""
    prior_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prior_close).abs(),
        (df["low"] - prior_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def _compute_baseline_stats(hist):
    """hist: a yfinance daily OHLCV DataFrame, most recent row = last
    COMPLETED trading day (today is never in this data)."""
    df = hist.copy()
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns=str.lower).sort_index()
    if len(df) < config.BREAKOUT_LOOKBACK_DAYS + 1:
        return None

    avg_volume_20d = df["volume"].rolling(20).mean().iloc[-1]
    high_20d = df["high"].rolling(config.BREAKOUT_LOOKBACK_DAYS).max().iloc[-1]
    turnover = (df["close"] * df["volume"]) / 1e7  # INR crore
    avg_turnover_cr_20d = turnover.rolling(20).mean().iloc[-1]

    rsi_series = _rsi_series(df["close"], config.RSI_PERIOD)
    rsi_14 = rsi_series.iloc[-1]
    prev_rsi_14 = rsi_series.iloc[-2] if len(rsi_series) > 1 else None

    prev_close = df["close"].iloc[-1]

    # ATR-14: average true range, used for volatility-scaled target/stop-loss
    atr_series = _atr_series(df, config.ATR_PERIOD)
    atr_14 = atr_series.iloc[-1]

    for val in (avg_volume_20d, high_20d, avg_turnover_cr_20d, rsi_14, prev_close):
        if pd.isna(val):
            return None

    return {
        "avg_volume_20d": float(avg_volume_20d),
        "high_20d": float(high_20d),
        "avg_turnover_cr_20d": float(avg_turnover_cr_20d),
        "rsi_14": float(rsi_14),
        "prev_rsi_14": float(prev_rsi_14) if prev_rsi_14 is not None and not pd.isna(prev_rsi_14) else None,
        "prev_close": float(prev_close),
        "atr_14": float(atr_14) if not pd.isna(atr_14) else None,
    }


# ---------------------------------------------------------------------------
# Long-term (up to config.LONG_HISTORY_YEARS) historical stats: pivot
# support/resistance levels, a long-window ATR, and a per-symbol mini
# backtest of "what happened after past setups like today's" — used by
# screener.py to ground entry/target/stop-loss in this specific stock's own
# history rather than a generic multiplier. Refreshed weekly (see
# LONG_TERM_CACHE_FILE below), not daily — a decade-plus of history and its
# derived stats don't meaningfully change day to day, and re-downloading it
# daily for the whole universe would be a needless load on Yahoo.
# ---------------------------------------------------------------------------
LONG_TERM_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "long_term_cache.json")


def _load_long_term_cache():
    if not os.path.exists(LONG_TERM_CACHE_FILE):
        return {}
    try:
        with open(LONG_TERM_CACHE_FILE) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    cached_date = cache.get("date")
    if not cached_date:
        return {}
    try:
        age_days = (datetime.now(_IST).date() - datetime.fromisoformat(cached_date).date()).days
    except ValueError:
        return {}
    if age_days >= config.LONG_TERM_CACHE_REFRESH_DAYS:
        return {}
    return cache.get("data", {})


def _save_long_term_cache(data):
    os.makedirs(os.path.dirname(LONG_TERM_CACHE_FILE), exist_ok=True)
    with open(LONG_TERM_CACHE_FILE, "w") as f:
        json.dump({"date": _today_ist_str(), "data": data}, f, indent=2)


def _cluster_levels(levels, cluster_pct):
    """Collapse a list of raw pivot price levels into representative zones —
    e.g. five separate pivot highs within 2% of each other become one
    resistance level — so nearby-level lookups aren't misled by noise."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters = [[levels[0]]]
    for lv in levels[1:]:
        if (lv - clusters[-1][-1]) / clusters[-1][-1] * 100 <= cluster_pct:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    return [sum(c) / len(c) for c in clusters]


def _detect_pivots(df, window):
    """A bar is a pivot high/low if its high/low is the max/min within a
    +/- `window` trading-day span around it. Simple, well-established swing-
    point definition; deliberately not fancier than that."""
    span = window * 2 + 1
    if len(df) < span:
        return [], []
    is_pivot_high = df["high"] == df["high"].rolling(span, center=True).max()
    is_pivot_low = df["low"] == df["low"].rolling(span, center=True).min()
    resistances = df.loc[is_pivot_high.fillna(False), "high"].tolist()
    supports = df.loc[is_pivot_low.fillna(False), "low"].tolist()
    return resistances, supports


def _breakout_backtest_stats(df):
    """The core "learn from this stock's own past" piece: walk its full
    history and find every past day that met the SAME two objective checks
    used in today's live signal (N-day breakout + RSI momentum — see
    screener.py checks 3 & 4), then measure what actually happened over the
    following BREAKOUT_BACKTEST_FORWARD_DAYS trading days. Returns the
    median forward return and median forward drawdown across all such
    occurrences, plus the sample size so callers can judge how much to
    trust it (see BREAKOUT_BACKTEST_MIN_SAMPLES)."""
    close = df["close"]
    # shift(1): "N-day high" must be computed from days STRICTLY BEFORE the
    # setup day, exactly mirroring how the live scanner compares today's LTP
    # against yesterday's high_20d baseline (never including today itself).
    high_n = close.rolling(config.BREAKOUT_LOOKBACK_DAYS).max().shift(1)
    rsi = _rsi_series(close, config.RSI_PERIOD)
    is_setup = (close > high_n) & (rsi >= config.RSI_MOMENTUM_MIN)

    n = len(df)
    horizon = config.BREAKOUT_BACKTEST_FORWARD_DAYS
    fwd_returns, fwd_drawdowns = [], []
    setup_positions = np.flatnonzero(is_setup.fillna(False).to_numpy())
    for i in setup_positions:
        if i + horizon >= n:
            continue
        entry_px = close.iloc[i]
        if not entry_px or pd.isna(entry_px):
            continue
        fwd_close = close.iloc[i + horizon]
        window_low = df["low"].iloc[i + 1: i + horizon + 1].min()
        fwd_returns.append((fwd_close / entry_px - 1) * 100)
        fwd_drawdowns.append((window_low / entry_px - 1) * 100)

    sample_size = len(fwd_returns)
    if sample_size < config.BREAKOUT_BACKTEST_MIN_SAMPLES:
        return {"sample_size": sample_size, "median_return_pct": None, "median_drawdown_pct": None}
    return {
        "sample_size": sample_size,
        "median_return_pct": float(statistics.median(fwd_returns)),
        "median_drawdown_pct": float(statistics.median(fwd_drawdowns)),
    }


def _compute_long_term_stats(hist):
    df = hist.copy()
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns=str.lower).sort_index()
    df = df.dropna(subset=["high", "low", "close"])
    if len(df) < 60:
        return None  # too little history for any of this to mean anything

    all_time_high = float(df["high"].max())
    last_252 = df.tail(252)
    high_52w = float(last_252["high"].max()) if not last_252.empty else None

    atr_series = _atr_series(df, config.LONG_TERM_ATR_PERIOD)
    atr_252 = float(atr_series.iloc[-1]) if not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else None

    raw_res, raw_sup = _detect_pivots(df, config.PIVOT_WINDOW_DAYS)
    resistances = _cluster_levels(raw_res, config.PIVOT_CLUSTER_PCT)
    supports = _cluster_levels(raw_sup, config.PIVOT_CLUSTER_PCT)

    backtest = _breakout_backtest_stats(df)

    return {
        "all_time_high": all_time_high,
        "high_52w": high_52w,
        "atr_252": atr_252,
        "resistances": resistances,
        "supports": supports,
        "breakout_sample_size": backtest["sample_size"],
        "breakout_median_return_pct": backtest["median_return_pct"],
        "breakout_median_drawdown_pct": backtest["median_drawdown_pct"],
        "history_years": round(len(df) / 252, 1),
    }


def fetch_long_term_stats_batch(symbols, years=None):
    """Returns {symbol: long_term_stats_dict}. Weekly-cached — see module
    docstring above."""
    years = years or config.LONG_HISTORY_YEARS
    cache = _load_long_term_cache()
    if cache:
        log.info("Using cached long-term stats (%d symbols, refreshed within the last %d days)",
                  len(cache), config.LONG_TERM_CACHE_REFRESH_DAYS)
        return cache

    to_date = datetime.now()
    from_date = to_date - timedelta(days=int(years * 365.25))
    tickers = [f"{s}.NS" for s in symbols]

    raw = None
    for attempt in range(1, config.YF_BATCH_RETRY_ATTEMPTS + 1):
        log.info("Fetching %s years of history for %d symbols (attempt %d/%d)",
                  years, len(symbols), attempt, config.YF_BATCH_RETRY_ATTEMPTS)
        try:
            raw = yf.download(
                tickers=tickers,
                start=from_date.strftime("%Y-%m-%d"),
                end=to_date.strftime("%Y-%m-%d"),
                group_by="ticker",
                progress=False,
                auto_adjust=False,
                threads=True,
            )
            if raw is not None and not raw.empty:
                break
        except Exception:
            log.exception("Batched long-history yfinance download failed (attempt %d)", attempt)
        if attempt < config.YF_BATCH_RETRY_ATTEMPTS:
            time.sleep(config.YF_BATCH_RETRY_BACKOFF_SEC * attempt)

    stats = {}
    if raw is not None and not raw.empty:
        for symbol in symbols:
            ticker = f"{symbol}.NS"
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    hist = raw[ticker].dropna(how="all")
                else:
                    hist = raw.dropna(how="all")
            except Exception:
                continue
            if hist is None or hist.empty:
                continue
            try:
                s = _compute_long_term_stats(hist)
            except Exception:
                log.exception("Failed computing long-term stats for %s", symbol)
                continue
            if s:
                stats[symbol] = s

    coverage_pct = (len(stats) / len(symbols) * 100) if symbols else 0
    log.info("Long-term stats computed for %d/%d symbols (%.0f%% coverage)",
              len(stats), len(symbols), coverage_pct)

    if coverage_pct >= config.YF_MIN_COVERAGE_PCT:
        _save_long_term_cache(stats)
    else:
        log.warning("Long-term stats coverage below %.0f%% — not caching, will retry next scan cycle",
                     config.YF_MIN_COVERAGE_PCT)

    return stats


def fetch_yf_baseline_batch(symbols, lookback_days=90):
    """
    Returns {symbol: baseline_stats_dict}, cached to disk for the calendar
    day (IST) so Yahoo Finance is only hit once per day regardless of how
    many times the scanner runs.

    Downloads all symbols in a single batched yfinance call (with retries)
    rather than ~100+ sequential single-symbol requests — Yahoo Finance
    intermittently rate-limits/blocks rapid sequential requests, especially
    from shared cloud IPs like GitHub Actions runners, which shows up as
    spurious "possibly delisted; no timezone found" errors on symbols that
    are obviously fine. If the batch still comes back badly incomplete, the
    partial result is NOT cached, so the next scan cycle retries instead of
    being stuck with missing symbols for the rest of the day.
    """
    cache = _load_yf_cache()
    if cache:
        log.info("Using cached Yahoo Finance baseline (%d symbols) for %s", len(cache), _today_ist_str())
        return cache

    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)
    tickers = [f"{s}.NS" for s in symbols]

    raw = None
    for attempt in range(1, config.YF_BATCH_RETRY_ATTEMPTS + 1):
        log.info("Fetching Yahoo Finance baseline for %d symbols (attempt %d/%d)",
                 len(symbols), attempt, config.YF_BATCH_RETRY_ATTEMPTS)
        try:
            raw = yf.download(
                tickers=tickers,
                start=from_date.strftime("%Y-%m-%d"),
                end=to_date.strftime("%Y-%m-%d"),
                group_by="ticker",
                progress=False,
                auto_adjust=False,
                threads=True,
            )
            if raw is not None and not raw.empty:
                break
        except Exception:
            log.exception("Batched yfinance download failed (attempt %d)", attempt)
        if attempt < config.YF_BATCH_RETRY_ATTEMPTS:
            time.sleep(config.YF_BATCH_RETRY_BACKOFF_SEC * attempt)

    baseline = {}
    if raw is not None and not raw.empty:
        for symbol in symbols:
            ticker = f"{symbol}.NS"
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    hist = raw[ticker].dropna(how="all")
                else:
                    # Only one ticker in the whole universe — flat columns
                    hist = raw.dropna(how="all")
            except Exception:
                continue
            if hist is None or hist.empty:
                continue
            stats = _compute_baseline_stats(hist)
            if stats:
                baseline[symbol] = stats

    coverage_pct = (len(baseline) / len(symbols) * 100) if symbols else 0
    log.info("Baseline computed for %d/%d symbols (%.0f%% coverage)",
              len(baseline), len(symbols), coverage_pct)

    if coverage_pct >= config.YF_MIN_COVERAGE_PCT:
        _save_yf_cache(baseline)
    else:
        log.warning("Coverage below %.0f%% threshold — not caching, will retry next scan cycle",
                    config.YF_MIN_COVERAGE_PCT)

    return baseline
