"""
Backtests the EXACT live screening logic (data_fetcher._compute_baseline_stats
+ screener.evaluate — imported and called directly, not reimplemented) against
real historical data, so you can see how often it would actually have fired
and roughly how those signals would have played out.

READ THIS BEFORE TRUSTING THE NUMBERS:
- Equities only. The F&O long-buildup check needs live open interest, which
  has no free historical source, so it can't be backtested — this only tests
  the SCAN_EQUITIES path. Since F&O evaluation reuses the same equity
  baseline for its own technical checks, equity signal frequency is a
  reasonable proxy for how often the underlying SETUP occurs, but the actual
  count of emailed alerts (EQ + multiple FUT expiries) could run higher.
- Uses each day's actual close/volume as a stand-in for "today's live
  snapshot." The live scanner sees partial-day volume/price building through
  the session; backtesting can only use end-of-day figures, so a signal
  might have fired at a different point intraday in reality.
- Fill simulation: a "current market price" entry is assumed filled the same
  day. A pullback entry (extended-stock case) only counts as filled if price
  actually trades down to that level within ENTRY_FILL_WINDOW_DAYS — if it
  never pulls back, that signal is recorded as "not_filled", a real and
  common outcome for a limit-style entry.
- "Win" = target hit before stop-loss within FORWARD_WINDOW_DAYS of fill,
  checked day-by-day using each day's high/low. If a single day's range
  covers both target and stop, we can't tell which happened first from daily
  bars alone — recorded as "ambiguous_same_day" rather than guessed.
- This estimates historical frequency and hit-rate of a rule-based screen.
  It is not a promise about future performance.

Usage:
    python backtest.py --months 6
    python backtest.py --months 12 --min-score 2      # loosen threshold to compare
"""
import argparse
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import config
import data_fetcher
import screener

ENTRY_FILL_WINDOW_DAYS = 5
FORWARD_WINDOW_DAYS = 10
MIN_HISTORY_ROWS = 260  # ~1 trading year, needed before baseline stats are meaningful


def download_full_history(symbols, years_back):
    tickers = [f"{s}.NS" for s in symbols]
    end = datetime.now()
    start = end - timedelta(days=years_back * 365)
    print(f"Downloading {len(symbols)} symbols, {years_back}y history each...")
    raw = yf.download(
        tickers=tickers,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        group_by="ticker",
        progress=False,
        auto_adjust=False,
        threads=True,
    )
    out = {}
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
        df = hist.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.rename(columns=str.lower).sort_index()
        out[symbol] = df
    return out


def simulate_symbol(symbol, df, start_idx):
    """
    Walks each day in [start_idx, len(df)-1). At day i, baseline is computed
    from df.iloc[:i] (everything strictly before day i — identical to how
    the live scanner treats "yesterday and earlier"), and day i's own
    close/volume stand in for "today's live snapshot". This reuses the
    actual production functions, so results reflect the real logic exactly.
    """
    results = []
    n = len(df)
    for i in range(start_idx, n - 1):
        baseline = data_fetcher._compute_baseline_stats(df.iloc[:i])
        if not baseline:
            continue
        today_row = df.iloc[i]
        live = {"ltp": float(today_row["close"]), "volume": float(today_row["volume"]), "oi": None}

        result = screener.evaluate(symbol, baseline, live, instrument="EQ")
        if not result:
            continue

        result["_signal_date"] = df.index[i]
        result["_signal_idx"] = i
        results.append(result)
    return results


def resolve_outcome(df, signal_idx, result):
    entry, target, stop = result["entry"], result["target"], result["stop_loss"]
    is_pullback = result["entry_note"] != "current market price"
    n = len(df)

    fill_idx = signal_idx
    if is_pullback:
        fill_idx = None
        for j in range(signal_idx, min(signal_idx + ENTRY_FILL_WINDOW_DAYS, n)):
            if df.iloc[j]["low"] <= entry:
                fill_idx = j
                break
        if fill_idx is None:
            return "not_filled", None

    for j in range(fill_idx, min(fill_idx + FORWARD_WINDOW_DAYS, n)):
        row = df.iloc[j]
        hit_target = row["high"] >= target
        hit_stop = row["low"] <= stop
        if hit_target and hit_stop:
            return "ambiguous_same_day", j - fill_idx
        if hit_target:
            return "win", j - fill_idx
        if hit_stop:
            return "loss", j - fill_idx
    return "open_at_window_end", FORWARD_WINDOW_DAYS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6, help="how many months back to test")
    parser.add_argument("--min-score", type=int, default=None,
                         help="override MIN_SIGNAL_SCORE for this run (default: use config.py)")
    args = parser.parse_args()

    if args.min_score is not None:
        config.MIN_SIGNAL_SCORE = args.min_score

    universe = data_fetcher.load_universe()
    all_hist = download_full_history(universe, config.LONG_HISTORY_YEARS)
    print(f"Got history for {len(all_hist)}/{len(universe)} symbols\n")

    all_signals = []
    for symbol, df in all_hist.items():
        if len(df) < MIN_HISTORY_ROWS:
            continue
        backtest_days = int(args.months * 21)  # ~21 trading days/month
        start_idx = max(MIN_HISTORY_ROWS, len(df) - backtest_days)
        sigs = simulate_symbol(symbol, df, start_idx)
        for r in sigs:
            outcome, days = resolve_outcome(df, r["_signal_idx"], r)
            r["_outcome"] = outcome
            r["_days_to_outcome"] = days
        all_signals.extend(sigs)

    total = len(all_signals)
    favorable = [r for r in all_signals if r["favorable"]]

    print(f"=== Backtest: last {args.months} months, {len(all_hist)} symbols (equities only) ===")
    print(f"Total signals (score >= {config.MIN_SIGNAL_SCORE}): {total}")
    print(f"Favorable / would-be-emailed (R:R >= {config.MIN_RISK_REWARD_TO_ALERT}): {len(favorable)}")

    if favorable:
        per_month = len(favorable) / args.months
        print(f"  -> approx {per_month:.1f} emailed equity signals/month across the whole universe\n")

        outcomes = pd.Series([r["_outcome"] for r in favorable]).value_counts()
        print("Outcome breakdown (of favorable/emailed signals):")
        print(outcomes.to_string())

        wins = sum(1 for r in favorable if r["_outcome"] == "win")
        losses = sum(1 for r in favorable if r["_outcome"] == "loss")
        decided = wins + losses
        if decided:
            print(f"\nWin rate (target hit before stop, excl. not-yet-decided/ambiguous): "
                  f"{wins}/{decided} = {wins/decided*100:.0f}%")
    else:
        print("No favorable signals in this window — try --months 12, or --min-score 2 to loosen the screen.")

    if all_signals:
        detail = pd.DataFrame([{
            "date": r["_signal_date"], "symbol": r["symbol"], "score": r["score"],
            "favorable": r["favorable"], "entry": r["entry"], "entry_note": r["entry_note"],
            "target": r["target"], "target_basis": r["target_basis"],
            "stop_loss": r["stop_loss"], "stop_basis": r["stop_basis"],
            "risk_reward": r["risk_reward"], "outcome": r.get("_outcome"),
            "days_to_outcome": r.get("_days_to_outcome"),
        } for r in all_signals])
        detail.to_csv("backtest_results.csv", index=False)
        print(f"\nFull detail written to backtest_results.csv ({len(detail)} rows)")


if __name__ == "__main__":
    main()
