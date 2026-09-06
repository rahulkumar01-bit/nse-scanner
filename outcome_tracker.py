"""
Tracks REAL, live alert outcomes over time — complementary to backtest.py's
historical simulation, not a replacement for it. Every time run_scan() fires
an alert, it's recorded here with its entry/target/stop. On each later scan
cycle, any recorded alert whose ~HOLDING_PERIOD_DAYS trading-day window has
elapsed gets "resolved" by fetching the real subsequent price action and
checking whether target or stop was actually touched (or timed out) —
producing genuine forward-tested statistics, not backtested ones.

Equity-only for now, matching backtest.py's own scope (F&O/OI alerts don't
have a simple daily-candle history to check against).
"""
import json
import logging
import os
import statistics
from datetime import datetime, timedelta

import yfinance as yf

import config

log = logging.getLogger("nse_scanner.outcome_tracker")

OUTCOMES_FILE = os.path.join(os.path.dirname(__file__), "data", "live_outcomes.json")


def _load():
    if not os.path.exists(OUTCOMES_FILE):
        return {"pending": [], "resolved": []}
    try:
        with open(OUTCOMES_FILE) as f:
            data = json.load(f)
        data.setdefault("pending", [])
        data.setdefault("resolved", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"pending": [], "resolved": []}


def _save(data):
    os.makedirs(os.path.dirname(OUTCOMES_FILE), exist_ok=True)
    with open(OUTCOMES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def record_alert(alert):
    """Call once per fired equity alert (from main.run_scan) to start
    tracking its real-world outcome. Silently skips non-equity alerts."""
    if alert.get("instrument") != "EQ":
        return
    data = _load()
    # Avoid double-recording the exact same alert if a scan cycle somehow re-fires
    # before state_store's own dedupe window would normally prevent it.
    key = (alert["symbol"], alert["date"])
    if any((p["symbol"], p["date"]) == key for p in data["pending"]):
        return
    data["pending"].append({
        "symbol": alert["symbol"],
        "instrument": alert["instrument"],
        "date": alert["date"],
        "entry": alert["entry"],
        "target": alert["target"],
        "stop_loss": alert["stop_loss"],
        "score": alert["score"],
        "risk_reward": alert.get("risk_reward"),
        "extended": alert.get("extended"),
    })
    _save(data)
    log.info("Recording live outcome tracking for %s (alerted %s)", alert["symbol"], alert["date"])


def resolve_pending_outcomes():
    """Checks every pending alert whose holding window has plausibly
    elapsed, fetches real subsequent price action, and records the real
    outcome (target/stop/timeout + actual return). Safe to call every scan
    cycle — a no-op for alerts that aren't due yet, and failures on any
    individual symbol just get retried next cycle rather than blocking
    the rest."""
    data = _load()
    if not data["pending"]:
        return

    still_pending = []
    newly_resolved = []
    today = datetime.now().date()

    for alert in data["pending"]:
        alert_date = datetime.strptime(alert["date"], "%Y-%m-%d").date()
        # Rough overestimate (accounts for weekends/holidays) so we don't even
        # try fetching before enough calendar time has plausibly passed.
        if (today - alert_date).days < config.HOLDING_PERIOD_DAYS * 1.5:
            still_pending.append(alert)
            continue

        try:
            outcome = _resolve_single(alert)
        except Exception:
            log.exception("Failed to resolve live outcome for %s (%s) — will retry next cycle",
                           alert["symbol"], alert["date"])
            still_pending.append(alert)
            continue

        if outcome is None:
            still_pending.append(alert)  # not enough real trading days have posted yet — retry later
            continue

        alert.update(outcome)
        newly_resolved.append(alert)

    data["pending"] = still_pending
    data["resolved"].extend(newly_resolved)
    _save(data)

    if newly_resolved:
        log.info("Resolved %d live alert outcome(s): %s", len(newly_resolved),
                  ", ".join(f"{a['symbol']} {a['outcome']} ({a['return_pct']:+.2f}%)" for a in newly_resolved))


def _resolve_single(alert):
    ticker = f"{alert['symbol']}.NS"
    start = datetime.strptime(alert["date"], "%Y-%m-%d")
    end = start + timedelta(days=int(config.HOLDING_PERIOD_DAYS * 2.2))  # generous buffer for weekends/holidays

    hist = yf.download(ticker, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                        progress=False, auto_adjust=False)
    if hist is None or hist.empty:
        return None
    hist.columns = [c[0] if isinstance(c, tuple) else c for c in hist.columns]
    hist = hist.rename(columns=str.lower).sort_index()
    hist = hist[hist.index.date > start.date()]  # only days AFTER the alert
    if len(hist) < config.HOLDING_PERIOD_DAYS:
        return None  # not enough real trading days have elapsed yet — retry later

    hist = hist.iloc[:config.HOLDING_PERIOD_DAYS]
    entry, target, stop = alert["entry"], alert["target"], alert["stop_loss"]

    # Same conservative same-day-ambiguity convention as backtest.py: if a
    # single day's range spans both target and stop, assume the stop wins.
    for i in range(len(hist)):
        lo, hi = hist["low"].iloc[i], hist["high"].iloc[i]
        if lo <= stop:
            return {"outcome": "stop", "return_pct": (stop / entry - 1) * 100,
                    "days_held": i + 1, "resolved_date": str(hist.index[i].date())}
        if hi >= target:
            return {"outcome": "target", "return_pct": (target / entry - 1) * 100,
                    "days_held": i + 1, "resolved_date": str(hist.index[i].date())}

    exit_close = hist["close"].iloc[-1]
    return {"outcome": "timeout", "return_pct": (exit_close / entry - 1) * 100,
            "days_held": len(hist), "resolved_date": str(hist.index[-1].date())}


def summarize_resolved(min_n=5):
    """Short human-readable summary of real, forward-tested performance so
    far — or None if there isn't enough resolved data yet to say anything
    meaningful. Intended for a one-line footer in alert emails."""
    data = _load()
    resolved = data["resolved"]
    if len(resolved) < min_n:
        return None
    returns = [r["return_pct"] for r in resolved]
    wins = sum(1 for r in resolved if r["outcome"] == "target")
    losses = sum(1 for r in resolved if r["outcome"] == "stop")
    timeouts = sum(1 for r in resolved if r["outcome"] == "timeout")
    return (
        f"Live tracker so far: {len(resolved)} alert(s) resolved — "
        f"target hit {wins} ({wins/len(resolved)*100:.0f}%), stopped {losses} ({losses/len(resolved)*100:.0f}%), "
        f"timed out {timeouts} ({timeouts/len(resolved)*100:.0f}%). "
        f"Avg return {statistics.mean(returns):+.2f}%, median {statistics.median(returns):+.2f}%. "
        f"({len(data['pending'])} still pending.)"
    )
