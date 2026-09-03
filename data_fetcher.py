"""
Pulls the instrument master + daily OHLCV history from Kotak Neo and turns
it into a tidy pandas DataFrame per symbol, with the indicators the
screener needs (avg volume, ATH/N-day high, RSI) already computed.
"""
import logging
from datetime import datetime, timedelta

import pandas as pd

import config

log = logging.getLogger("nse_scanner.data_fetcher")


def load_universe():
    df = pd.read_csv(config.UNIVERSE_FILE)
    return [s.strip().upper() for s in df["symbol"].tolist()]


def build_token_map(kc, symbols, exchange_segment="nse_cm"):
    """
    Resolves each symbol to its instrument token via search_scrip() (which
    is cached client-side per day by the SDK, so this doesn't hit the
    network hard even for a large universe). Returns {symbol: instrument_token}.
    """
    token_map = {}
    for symbol in symbols:
        try:
            results = kc.search_scrip(exchange_segment=exchange_segment, symbol=symbol)
        except Exception:
            log.exception("search_scrip failed for %s", symbol)
            continue

        for row in results or []:
            trd_symbol = (row.get("pTrdSymbol") or "").upper()
            sym_name = (row.get("pSymbolName") or "").upper()
            group = (row.get("pGroup") or "").upper()
            token = row.get("pSymbol")
            # Prefer the plain equity listing (trading symbol "<SYM>-EQ" / group "EQ"),
            # skipping variants like -BE/-BL/-BZ series.
            if sym_name == symbol and token and (trd_symbol == f"{symbol}-EQ" or group == "EQ"):
                token_map[symbol] = token
                break
    return token_map


def get_fno_token(kc, underlying_symbol):
    """
    Resolves the nearest-expiry futures instrument token for an underlying
    via search_scrip(..., option_type="FUT"), picking the closest unexpired
    contract from the results.
    """
    try:
        results = kc.search_scrip(exchange_segment="nse_fo", symbol=underlying_symbol, option_type="FUT")
    except Exception:
        log.exception("search_scrip (FUT) failed for %s", underlying_symbol)
        return None
    if not results:
        return None

    def expiry_epoch(row):
        try:
            return float(row.get("lExpiryDate"))
        except (TypeError, ValueError):
            return float("inf")

    now_epoch = datetime.now().timestamp()
    future = [r for r in results if expiry_epoch(r) >= now_epoch]
    future.sort(key=expiry_epoch)
    chosen = future[0] if future else None
    return chosen.get("pSymbol") if chosen else None


def fetch_daily_history(kc, instrument_token, exchange_segment="nse_cm", lookback_days=90):
    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)
    raw = kc.historical_data(
        symbol=None,
        exchange_segment=exchange_segment,
        instrument_token=instrument_token,
        interval="1day",
        from_date=from_date.strftime("%Y-%m-%d"),
        to_date=to_date.strftime("%Y-%m-%d"),
    )
    candles = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    # Normalize expected columns across possible SDK field-naming variants
    rename_map = {}
    for col in df.columns:
        lc = col.lower()
        if lc in ("time", "timestamp", "date"):
            rename_map[col] = "date"
        elif lc in ("open", "o"):
            rename_map[col] = "open"
        elif lc in ("high", "h"):
            rename_map[col] = "high"
        elif lc in ("low", "l"):
            rename_map[col] = "low"
        elif lc in ("close", "c"):
            rename_map[col] = "close"
        elif lc in ("volume", "v"):
            rename_map[col] = "volume"
    df = df.rename(columns=rename_map)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def compute_indicators(df):
    """Adds avg_volume_20d, high_20d, rsi_14, turnover_cr_20d columns."""
    if df.empty or len(df) < config.BREAKOUT_LOOKBACK_DAYS + 1:
        return df

    df = df.copy()
    df["avg_volume_20d"] = df["volume"].rolling(20).mean().shift(1)
    df["high_20d"] = df["high"].rolling(config.BREAKOUT_LOOKBACK_DAYS).max().shift(1)
    df["turnover_cr"] = (df["close"] * df["volume"]) / 1e7  # INR crore
    df["avg_turnover_cr_20d"] = df["turnover_cr"].rolling(20).mean().shift(1)

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(config.RSI_PERIOD).mean()
    avg_loss = loss.rolling(config.RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    df["pct_change"] = df["close"].pct_change() * 100
    df["volume_ratio"] = df["volume"] / df["avg_volume_20d"]
    return df
