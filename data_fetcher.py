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
from datetime import datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

import config

log = logging.getLogger("nse_scanner.data_fetcher")

_IST = pytz.timezone(config.TIMEZONE)
YF_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "yf_cache.json")


def load_universe():
    df = pd.read_csv(config.UNIVERSE_FILE)
    return [s.strip().upper() for s in df["symbol"].tolist()]


def _extract_rows(payload):
    """
    Several Kotak endpoints (search_scrip, quotes) should return a bare list
    per their docs, but in practice the response is sometimes wrapped in a
    dict (e.g. {"data": [...]}) depending on SDK/response-format version.
    Unwrap defensively instead of assuming either shape.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "result", "results", "scrips", "list", "message",
                    "Success", "success"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
        log.warning("Unrecognized dict response shape: keys=%s", list(payload.keys()))
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


def fetch_yf_baseline_batch(symbols, lookback_days=90):
    """
    Returns {symbol: baseline_stats_dict}, cached to disk for the calendar
    day (IST) so Yahoo Finance is only hit once per day regardless of how
    many times the scanner runs.
    """
    cache = _load_yf_cache()
    if cache:
        log.info("Using cached Yahoo Finance baseline (%d symbols) for %s", len(cache), _today_ist_str())
        return cache

    log.info("Fetching fresh Yahoo Finance baseline for %d symbols", len(symbols))
    baseline = {}
    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)
    for symbol in symbols:
        try:
            hist = yf.download(
                f"{symbol}.NS",
                start=from_date.strftime("%Y-%m-%d"),
                end=to_date.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=False,
            )
        except Exception:
            log.exception("yfinance download failed for %s", symbol)
            continue
        if hist is None or hist.empty:
            log.warning("No Yahoo Finance data for %s.NS", symbol)
            continue

        stats = _compute_baseline_stats(hist)
        if stats:
            baseline[symbol] = stats

    _save_yf_cache(baseline)
    log.info("Baseline computed for %d/%d symbols", len(baseline), len(symbols))
    return baseline
