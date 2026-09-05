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
import time
from datetime import datetime, timedelta

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

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(config.RSI_PERIOD).mean()
    avg_loss = loss.rolling(config.RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi_series = 100 - (100 / (1 + rs))
    rsi_14 = rsi_series.iloc[-1]
    prev_rsi_14 = rsi_series.iloc[-2] if len(rsi_series) > 1 else None

    prev_close = df["close"].iloc[-1]

    # ATR-14: average true range, used for volatility-scaled target/stop-loss
    prior_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prior_close).abs(),
        (df["low"] - prior_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = true_range.rolling(config.ATR_PERIOD).mean()
    atr_14 = atr_series.iloc[-1]

    # --- Entry-quality context: is the stock already extended? ---
    sma_50 = df["close"].rolling(50).mean().iloc[-1] if len(df) >= 50 else None
    ema_20 = df["close"].ewm(span=config.PULLBACK_EMA_PERIOD, adjust=False).mean().iloc[-1]

    # --- Stop-loss support: actual recent swing low, not just a formula ---
    swing_low_lookback = min(config.SWING_LOW_LOOKBACK_DAYS, len(df))
    swing_low = df["low"].iloc[-swing_low_lookback:].min()

    # --- Target resistance candidates from real history, nearest above price wins ---
    def _period_high(trading_days):
        window = df["high"].iloc[-trading_days:] if len(df) >= 1 else df["high"]
        return float(window.max()) if len(window) else None

    high_52w = _period_high(252)
    high_3y = _period_high(756)
    high_5y = _period_high(1260)
    all_time_high = float(df["high"].max())

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
        "sma_50": float(sma_50) if sma_50 is not None and not pd.isna(sma_50) else None,
        "ema_20": float(ema_20) if not pd.isna(ema_20) else None,
        "swing_low": float(swing_low) if not pd.isna(swing_low) else None,
        "high_52w": high_52w,
        "high_3y": high_3y,
        "high_5y": high_5y,
        "all_time_high": all_time_high,
    }


def fetch_yf_baseline_batch(symbols, lookback_days=None):
    """
    Returns {symbol: baseline_stats_dict}, cached to disk for the calendar
    day (IST) so Yahoo Finance is only hit once per day regardless of how
    many times the scanner runs.

    Pulls up to config.LONG_HISTORY_YEARS of daily history (default 15y) —
    needed for the 52-week/3y/5y/all-time-high resistance levels used in
    target/entry logic, not just the ~90 days the short-term indicators need.
    Recently-listed stocks simply get whatever shorter history exists;
    yfinance returns what's available rather than erroring.

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

    lookback_days = lookback_days or (config.LONG_HISTORY_YEARS * 365)
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
