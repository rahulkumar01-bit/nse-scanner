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


def build_token_map(kc, exchange_segment="nse_cm"):
    """
    Downloads the scrip master and returns {symbol: instrument_token} for
    the equity segment. F&O current-month futures tokens are resolved
    separately in get_fno_token() since they roll over monthly.
    """
    master = kc.scrip_master(exchange_segment=exchange_segment)
    # The master comes back as a list of dicts (field names per Kotak's docs:
    # pSymbol / pTrdSymbol / pSymbolName / lLotSize etc. — normalize defensively).
    token_map = {}
    for row in master:
        sym = (row.get("pTrdSymbol") or row.get("pSymbolName") or row.get("symbol") or "").upper()
        # Kotak equity trading symbols often look like "RELIANCE-EQ"
        sym = sym.replace("-EQ", "")
        token = row.get("pSymbol") or row.get("instrument_token") or row.get("token")
        if sym and token:
            token_map[sym] = token
    return token_map


def get_fno_token(kc, underlying_symbol, expiry="current_month"):
    """
    Resolves the current-month futures instrument token for an underlying.
    Kotak's F&O master uses a similar shape to the equity one but with
    pOptionType/pExpiryDate/pInstType fields — filter for FUTSTK/FUTIDX
    with the nearest (unexpired) expiry.
    """
    master = kc.scrip_master(exchange_segment="nse_fo")
    candidates = [
        row for row in master
        if (row.get("pSymbolName") or "").upper() == underlying_symbol
        and (row.get("pInstType") or "").upper() in ("FUTSTK", "FUTIDX")
    ]
    if not candidates:
        return None
    # pick nearest expiry that hasn't passed
    def parse_expiry(row):
        try:
            return datetime.strptime(row.get("pExpiryDate", ""), "%d-%b-%Y")
        except Exception:
            return datetime.max

    today = datetime.now()
    future = [c for c in candidates if parse_expiry(c) >= today]
    future.sort(key=parse_expiry)
    chosen = future[0] if future else None
    if not chosen:
        return None
    return chosen.get("pSymbol") or chosen.get("token")


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
