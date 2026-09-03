"""
Composite momentum/breakout screener.

IMPORTANT: this is a heuristic technical scanner. It flags stocks showing
the kind of price/volume behaviour that has SOMETIMES preceded fast moves —
it does not predict returns, and a flagged stock is not a recommendation to
trade. Position sizing, stop-losses, and your own judgement are on you.

Each candidate is scored 0-5 on independent checks, combining yesterday's
baseline stats (from Yahoo Finance) with today's live snapshot (from
Kotak). A symbol is alerted when the score reaches config.MIN_SIGNAL_SCORE.
"""
from datetime import datetime

import pytz

import config

_IST = pytz.timezone(config.TIMEZONE)


def evaluate(symbol, baseline, live, instrument="EQ", oi_change_pct=None):
    """
    baseline: dict from data_fetcher._compute_baseline_stats() (yesterday
      and earlier — avg_volume_20d, high_20d, rsi_14, prev_rsi_14,
      avg_turnover_cr_20d, prev_close).
    live: dict from data_fetcher.fetch_live_quote() (today — ltp, volume, oi).
    Returns None if no signal, else a dict describing the alert.
    """
    if not baseline or not live:
        return None

    ltp = live.get("ltp")
    volume = live.get("volume")
    prev_close = baseline.get("prev_close")
    if ltp is None or not prev_close:
        return None

    if ltp < config.MIN_PRICE:
        return None
    if baseline.get("avg_turnover_cr_20d", 0) < config.MIN_AVG_TURNOVER_CR:
        return None

    pct_change = (ltp - prev_close) / prev_close * 100

    checks = {}

    # 1. Sharp move vs yesterday's close
    checks["day_move"] = pct_change >= config.DAY_CHANGE_PCT_THRESHOLD

    # 2. Volume surge vs 20-day average (volume-so-far today; naturally
    # rises through the day, so this check gets more meaningful later in
    # the session)
    avg_volume_20d = baseline.get("avg_volume_20d")
    volume_ratio = (volume / avg_volume_20d) if (volume and avg_volume_20d) else None
    checks["volume_surge"] = bool(volume_ratio and volume_ratio >= config.VOLUME_SURGE_MULTIPLE)

    # 3. Breakout above N-day high
    high_20d = baseline.get("high_20d")
    checks["breakout"] = bool(high_20d and ltp > high_20d)

    # 4. RSI momentum: yesterday's RSI already elevated and rising vs the
    # day before (an exact live-updated RSI would need today's final close,
    # which isn't known until end of day)
    rsi = baseline.get("rsi_14")
    prev_rsi = baseline.get("prev_rsi_14")
    checks["rsi_momentum"] = bool(
        rsi is not None and rsi >= config.RSI_MOMENTUM_MIN and (prev_rsi is None or rsi > prev_rsi)
    )

    # 5. Optional F&O confirmation: long buildup (price up + OI up vs the
    # last time we recorded it)
    if oi_change_pct is not None:
        checks["oi_buildup"] = oi_change_pct >= config.OI_CHANGE_PCT_THRESHOLD and checks["day_move"]
    else:
        checks["oi_buildup"] = False

    score = sum(1 for v in checks.values() if v)
    if score < config.MIN_SIGNAL_SCORE:
        return None

    reasons = []
    if checks["day_move"]:
        reasons.append(f"up {pct_change:.1f}% vs yesterday's close")
    if checks["volume_surge"]:
        reasons.append(f"volume already {volume_ratio:.1f}x the 20-day average")
    if checks["breakout"]:
        reasons.append(f"broke above its {config.BREAKOUT_LOOKBACK_DAYS}-day high")
    if checks["rsi_momentum"]:
        reasons.append(f"RSI(14) at {rsi:.0f} and rising (as of yesterday's close)")
    if checks["oi_buildup"]:
        reasons.append(f"open interest up {oi_change_pct:.1f}% (long buildup)")

    return {
        "symbol": symbol,
        "instrument": instrument,
        "close": round(ltp, 2),
        "pct_change": round(pct_change, 2),
        "score": score,
        "max_score": len(checks),
        "reasons": reasons,
        "date": datetime.now(_IST).strftime("%Y-%m-%d"),
    }
