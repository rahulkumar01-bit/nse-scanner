"""
Walk-forward backtest of the momentum screener over a trailing window
(default: 6 months), comparing:

  OLD method — flat ATR-multiplier target/stop, always "enter" at the live
              price (this is what screener.py did before the entry/target/
              stop-loss redesign).
  NEW method — extension-aware entry (proposes a pullback instead of
              chasing when the move already looks late-stage) and
              pattern/ATR-based target/stop, grounded in each stock's own
              historical behaviour where enough precedent exists.

Reuses the ACTUAL production functions (screener.evaluate, screener._propose_
entry/_propose_target_stop, data_fetcher._compute_long_term_stats etc.) —
this exercises the real code path, not a parallel reimplementation that
could silently drift from what actually runs live.

No lookahead: at simulated "day t", only price history strictly BEFORE day t
is used to compute the baseline and long-term stats — exactly mirroring how
the live scanner only ever sees yesterday's completed data plus today's
live tick. Long-term stats are recomputed every LONG_TERM_REFRESH_EVERY
trading days (mirroring the weekly production cache), not every single day —
both for realism and because recomputing a 15-year pivot/backtest analysis
for every day would be needlessly slow.

Needs network access to Yahoo Finance (yfinance) — run this locally or as a
GitHub Actions job. It will NOT run in a fully offline sandbox.

Usage:
    python backtest.py --months 6
    python backtest.py --months 6 --symbols RELIANCE,TCS,INFY   # quick subset
"""
import argparse
import json
import statistics
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import config
import data_fetcher
import screener

MAX_HOLDING_DAYS = 7         # matches the actual intended holding window (see config.HOLDING_PERIOD_DAYS) — time-based
                             # exit if neither target nor stop hits within this many trading days
FILL_WINDOW_DAYS = 3         # a pullback entry needs to fill fast to still be relevant for a ~week-long trade
LONG_TERM_REFRESH_EVERY = 5  # trading days between long-term-stats recomputation (mirrors the weekly production cache)


def _prep_history(hist):
    df = hist.copy()
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.rename(columns=str.lower).sort_index()
    return df.dropna(subset=["open", "high", "low", "close", "volume"])


def _rolling_baseline_series(df):
    """Vectorized equivalent of data_fetcher._compute_baseline_stats, as a
    full time series so per-day backtest lookups are just indexing."""
    return pd.DataFrame({
        "avg_volume_20d": df["volume"].rolling(20).mean(),
        "high_20d": df["high"].rolling(config.BREAKOUT_LOOKBACK_DAYS).max(),
        "avg_turnover_cr_20d": ((df["close"] * df["volume"]) / 1e7).rolling(20).mean(),
        "rsi_14": (rsi := data_fetcher._rsi_series(df["close"], config.RSI_PERIOD)),
        "prev_rsi_14": rsi.shift(1),
        "prev_close": df["close"],
        "atr_14": data_fetcher._atr_series(df, config.ATR_PERIOD),
    })


def _simulate_forward(df, fill_idx, entry, target, stop_loss):
    """Walk forward up to MAX_HOLDING_DAYS bars from fill_idx. Conservative
    convention: if a single day's range spans both target and stop, assume
    the stop triggers first (worst case, not best case)."""
    n = len(df)
    for offset in range(1, MAX_HOLDING_DAYS + 1):
        i = fill_idx + offset
        if i >= n:
            break
        lo, hi = df["low"].iloc[i], df["high"].iloc[i]
        if lo <= stop_loss:
            return "stop", (stop_loss / entry - 1) * 100, offset
        if hi >= target:
            return "target", (target / entry - 1) * 100, offset
    exit_idx = min(fill_idx + MAX_HOLDING_DAYS, n - 1)
    return "timeout", (df["close"].iloc[exit_idx] / entry - 1) * 100, exit_idx - fill_idx


def _find_fill(df, signal_idx, proposed_entry, ltp):
    """For a pullback entry (proposed_entry < ltp), look forward up to
    FILL_WINDOW_DAYS for the first day price actually traded down to it —
    a proposed pullback isn't a guaranteed fill in real trading, and the
    backtest shouldn't pretend otherwise."""
    if proposed_entry >= ltp - 1e-9:
        return signal_idx, proposed_entry  # immediate entry, same bar as the signal
    n = len(df)
    for offset in range(1, FILL_WINDOW_DAYS + 1):
        i = signal_idx + offset
        if i >= n:
            break
        if df["low"].iloc[i] <= proposed_entry:
            return i, proposed_entry
    return None, None


def old_target_stop(entry, atr, score):
    """Reproduces the pre-redesign flat ATR-multiplier method, so its
    hypothetical performance can be compared directly against the new one."""
    if atr:
        atr_multiplier = config.TARGET_ATR_BASE_MULTIPLIER + \
            max(0, score - config.MIN_SIGNAL_SCORE) * config.TARGET_ATR_SCORE_STEP
        target = max(entry + atr_multiplier * atr, entry * (1 + config.TARGET_RETURN_MIN_PCT / 100))
        stop_loss = entry - config.STOP_LOSS_ATR_MULTIPLIER * atr
    else:
        target = entry * (1 + config.TARGET_RETURN_MIN_PCT / 100)
        stop_loss = entry * (1 - config.STOP_LOSS_PCT_FALLBACK / 100)
    return target, max(stop_loss, 0.01)


def backtest_symbol(symbol, hist, months):
    df = _prep_history(hist)
    if len(df) < 300:
        return []  # not enough history for baseline + long-term stats to mean anything

    baseline_series = _rolling_baseline_series(df)
    cutoff = df.index.max() - pd.Timedelta(days=int(months * 30.44))
    start_idx = max(int(df.index.searchsorted(cutoff)), config.BREAKOUT_LOOKBACK_DAYS + 5)

    results = []
    long_term_snapshot, long_term_computed_at = None, -999

    for t in range(start_idx, len(df) - 1):
        if long_term_snapshot is None or t - long_term_computed_at >= LONG_TERM_REFRESH_EVERY:
            try:
                long_term_snapshot = data_fetcher._compute_long_term_stats(df.iloc[:t])
            except Exception:
                long_term_snapshot = None
            long_term_computed_at = t

        b = baseline_series.iloc[t - 1]
        if b.isna().any():
            continue
        baseline = b.to_dict()
        live = {"ltp": df["close"].iloc[t], "volume": df["volume"].iloc[t], "oi": None}

        sig = screener.evaluate(symbol, baseline, live, long_term=long_term_snapshot)
        if not sig:
            continue

        # NEW method — respect whether a proposed pullback would actually have filled
        fill_idx, fill_price = _find_fill(df, t, sig["entry"], live["ltp"])
        if fill_idx is not None:
            outcome, ret_pct, days = _simulate_forward(df, fill_idx, fill_price, sig["target"], sig["stop_loss"])
            new_result = {"filled": True, "outcome": outcome, "return_pct": ret_pct, "days_held": days}
        else:
            new_result = {"filled": False, "outcome": "no_fill", "return_pct": None, "days_held": None}

        # OLD method — always chases the live price immediately
        old_target, old_stop = old_target_stop(live["ltp"], baseline["atr_14"], sig["score"])
        old_outcome, old_ret, old_days = _simulate_forward(df, t, live["ltp"], old_target, old_stop)

        results.append({
            "symbol": symbol, "date": str(df.index[t].date()), "score": sig["score"],
            "extended": sig["extended"], "levels_basis": sig["levels_basis"],
            "new_entry": sig["entry"], "new_target": sig["target"], "new_stop": sig["stop_loss"],
            "new_filled": new_result["filled"], "new_outcome": new_result["outcome"],
            "new_return_pct": new_result["return_pct"], "new_days_held": new_result["days_held"],
            "old_entry": live["ltp"], "old_target": old_target, "old_stop": old_stop,
            "old_outcome": old_outcome, "old_return_pct": old_ret, "old_days_held": old_days,
        })

    return results


def summarize(results, label, key_prefix):
    filled = [r for r in results if r.get(f"{key_prefix}_return_pct") is not None]
    print(f"\n{label}")
    if not filled:
        print("  No filled signals.")
        return
    returns = [r[f"{key_prefix}_return_pct"] for r in filled]
    wins = sum(1 for r in filled if r[f"{key_prefix}_outcome"] == "target")
    losses = sum(1 for r in filled if r[f"{key_prefix}_outcome"] == "stop")
    timeouts = sum(1 for r in filled if r[f"{key_prefix}_outcome"] == "timeout")
    print(f"  Signals filled: {len(filled)} / {len(results)}")
    print(f"  Target hit: {wins} ({wins/len(filled)*100:.0f}%)  |  "
          f"Stopped out: {losses} ({losses/len(filled)*100:.0f}%)  |  "
          f"Timed out: {timeouts} ({timeouts/len(filled)*100:.0f}%)")
    print(f"  Avg return: {statistics.mean(returns):+.2f}%  |  Median: {statistics.median(returns):+.2f}%  |  "
          f"Best: {max(returns):+.2f}%  |  Worst: {min(returns):+.2f}%")
    compounded = 1.0
    for r in returns:
        compounded *= (1 + r / 100)
    print(f"  Equal-sized-bet cumulative multiple across all signals (no compounding logic beyond this): {compounded:.2f}x")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--symbols", type=str, default=None, help="comma-separated subset; default = full universe")
    parser.add_argument("--out", type=str, default="backtest_report.json")
    args = parser.parse_args()

    universe = args.symbols.split(",") if args.symbols else data_fetcher.load_universe()
    print(f"Backtesting {len(universe)} symbols over the last {args.months} months "
          f"(equity signals only — F&O/OI checks need futures history this backtest doesn't fetch)...")

    to_date = datetime.now()
    from_date = to_date - timedelta(days=int(config.LONG_HISTORY_YEARS * 365.25))
    tickers = [f"{s}.NS" for s in universe]
    raw = yf.download(tickers=tickers, start=from_date.strftime("%Y-%m-%d"), end=to_date.strftime("%Y-%m-%d"),
                       group_by="ticker", progress=True, auto_adjust=False, threads=True)

    all_results = []
    for symbol in universe:
        ticker = f"{symbol}.NS"
        try:
            hist = raw[ticker].dropna(how="all") if isinstance(raw.columns, pd.MultiIndex) else raw.dropna(how="all")
        except Exception:
            continue
        if hist is None or hist.empty:
            continue
        try:
            all_results.extend(backtest_symbol(symbol, hist, args.months))
        except Exception as e:
            print(f"  {symbol}: backtest error — {e}")

    print(f"\nTotal signals fired: {len(all_results)}")
    summarize(all_results, "OLD method (flat ATR multiplier, always chase LTP)", "old")
    summarize(all_results, "NEW method (extension-aware entry, pattern/ATR-based target-stop)", "new")

    extended = [r for r in all_results if r["extended"]]
    print(f"\n{len(extended)} of {len(all_results)} signals were flagged 'extended' "
          f"(pullback entry proposed instead of chasing).")
    if extended:
        fill_rate = sum(1 for r in extended if r["new_filled"]) / len(extended) * 100
        print(f"  Of those, {fill_rate:.0f}% actually got filled at the proposed pullback level within {FILL_WINDOW_DAYS} trading days.")

    with open(args.out, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nFull per-signal detail written to {args.out}")


if __name__ == "__main__":
    main()
