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


def _nearest_above(levels, price, max_pct):
    candidates = [lv for lv in (levels or []) if lv > price and (lv - price) / price * 100 <= max_pct]
    return min(candidates) if candidates else None


def _nearest_below(levels, price, max_pct):
    candidates = [lv for lv in (levels or []) if lv < price and (price - lv) / price * 100 <= max_pct]
    return max(candidates) if candidates else None


def _is_extended(pct_change, rsi, ltp, high_20d):
    """Heuristic check for "this runup already looks late-stage" — any one
    of an already-very-overbought RSI, an unusually large single-day spike,
    or price already running well past the breakout trigger (not just
    freshly through it) is treated as a caution sign."""
    if rsi is not None and rsi >= config.RSI_EXTENDED_THRESHOLD:
        return True
    if pct_change >= config.EXTENDED_DAY_MOVE_PCT:
        return True
    if high_20d and ltp > high_20d * (1 + config.EXTENDED_ABOVE_BREAKOUT_PCT / 100):
        return True
    return False


def _propose_entry(ltp, atr, high_20d, supports, extended):
    """Returns (entry_price, note). When the move doesn't look extended,
    entry is simply the live price — chasing isn't really "chasing" yet.
    When it does, prefer a real historical support/pivot level as a
    pullback entry over the live price; fall back to the breakout trigger
    level, then a plain ATR-sized pullback, in that order of preference."""
    if not extended:
        return ltp, "current price — the move doesn't look extended yet"

    support = _nearest_below(supports, ltp, config.SUPPORT_SEARCH_MAX_PCT_BELOW)
    if support:
        return support, (
            f"pullback entry near a historical support/pivot level (\u20b9{support:.2f}) "
            "rather than chasing here — the move already looks extended"
        )
    if high_20d and high_20d < ltp:
        return high_20d, (
            f"pullback entry near the recent breakout trigger level (\u20b9{high_20d:.2f}), "
            "which should now act as support — the move already looks extended"
        )
    if atr:
        px = ltp - config.PULLBACK_ATR_MULTIPLIER * atr
        return px, (
            f"pullback entry ~{config.PULLBACK_ATR_MULTIPLIER:g}x ATR below the current price "
            "(no clean historical support level found nearby) — the move already looks extended"
        )
    return ltp, "the move already looks extended, but no pullback reference was available — treat the current price with extra caution"


def _propose_target_stop(entry, atr, long_term, score, min_signal_score):
    """Prefer this stock's OWN historical behaviour (from a per-symbol
    backtest of similar past setups, over the actual HOLDING_PERIOD_DAYS
    window) when there's enough precedent to trust it; otherwise fall back
    to the ATR-multiplier method. The target is NOT capped at a fixed
    percentage or forced through a resistance ceiling when real historical
    precedent exists — it reflects this stock's own strong-but-real
    short-term outcome (see BREAKOUT_TARGET_PERCENTILE). Resistance only
    caps the target in the ATR-fallback case, where there's no stock-
    specific precedent to trust instead. The stop can only ever be
    TIGHTENED by a nearby support level, never widened — a support that
    happens to sit far below entry shouldn't blow out an otherwise
    well-sized stop (backtesting showed the old "replace outright" version
    of this rule was inflating average losses)."""
    long_term = long_term or {}
    resistances = long_term.get("resistances") or []
    supports = long_term.get("supports") or []
    sample_size = long_term.get("breakout_sample_size") or 0
    target_return_pct = long_term.get("breakout_target_return_pct")
    winners_mae_pct = long_term.get("breakout_winners_mae_pct")
    median_drawdown_pct = long_term.get("breakout_median_drawdown_pct")
    atr_252 = long_term.get("atr_252")

    # Stop-sizing bound, scaled to the actual ~HOLDING_PERIOD_DAYS holding
    # window (sqrt-of-time volatility scaling — a 6-day hold's typical price
    # range is roughly ATR * sqrt(6), not ATR itself, which is a single
    # day's typical range), using the current ATR-14 capped against the
    # long-term ATR-252 only to catch cases where recent volatility looks
    # anomalously (and probably temporarily) low.
    if atr and atr_252:
        daily_atr = min(atr_252, atr * config.LONG_ATR_MAX_RATIO_TO_SHORT)
    else:
        daily_atr = atr or atr_252
    holding_atr = daily_atr * (config.HOLDING_PERIOD_DAYS ** 0.5) if daily_atr else None
    min_dist = config.STOP_LOSS_MIN_ATR_MULTIPLIER * holding_atr if holding_atr else None
    max_dist = config.STOP_LOSS_MAX_ATR_MULTIPLIER * holding_atr if holding_atr else None

    if sample_size >= config.BREAKOUT_BACKTEST_MIN_SAMPLES and target_return_pct is not None:
        target = entry * (1 + max(target_return_pct, config.TARGET_RETURN_MIN_PCT) / 100)
        dd_basis = winners_mae_pct if winners_mae_pct is not None else median_drawdown_pct
        if min_dist is not None:
            drawdown_dist = abs(dd_basis) / 100 * entry if dd_basis is not None else min_dist
            stop_dist = min(max(drawdown_dist, min_dist), max_dist)
        else:
            stop_dist = entry * config.STOP_LOSS_PCT_FALLBACK / 100
        stop_loss = entry - stop_dist
        method = "pattern"
        basis = (f"this stock's own history — {sample_size} similar past setups, "
                 f"top-quartile outcome over ~{config.HOLDING_PERIOD_DAYS} trading days")
    elif atr:
        atr_multiplier = config.TARGET_ATR_BASE_MULTIPLIER + \
            max(0, score - min_signal_score) * config.TARGET_ATR_SCORE_STEP
        target = max(entry + atr_multiplier * atr, entry * (1 + config.TARGET_RETURN_MIN_PCT / 100))
        # No stock-specific precedent to trust here, so a nearby resistance
        # IS allowed to cap this target — see the pattern-based branch above
        # for why it's deliberately not applied there too.
        resistance = _nearest_above(resistances, entry, config.RESISTANCE_SEARCH_MAX_PCT_ABOVE)
        if resistance and entry < resistance < target:
            captured_pct = (resistance - entry) / (target - entry) * 100 if target > entry else 0
            if captured_pct >= config.RESISTANCE_CAP_MIN_CAPTURE_PCT:
                target = resistance
        stop_dist = config.STOP_LOSS_ATR_MULTIPLIER * atr
        stop_loss = entry - stop_dist
        method = "atr_fallback"
        basis = "ATR-based (not enough matching historical setups yet for a pattern-based estimate)"
    else:
        target = entry * (1 + config.TARGET_RETURN_MIN_PCT / 100)
        stop_dist = entry * config.STOP_LOSS_PCT_FALLBACK / 100
        stop_loss = entry - stop_dist
        method = "fixed_fallback"
        basis = "fallback percentage (no price history available)"

    # A nearby support can only TIGHTEN the stop to a cleaner structural
    # invalidation point — never widen it.
    support = _nearest_below(supports, entry, config.SUPPORT_SEARCH_MAX_PCT_BELOW)
    if support and min_dist is not None:
        structural_dist = entry - support * (1 - config.SUPPORT_BUFFER_PCT / 100)
        if min_dist <= structural_dist < stop_dist:
            stop_loss = entry - structural_dist
            basis += "; stop tightened to just below a nearby support level"

    return target, stop_loss, basis, method


def evaluate(symbol, baseline, live, instrument="EQ", oi_change_pct=None, long_term=None):
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

    atr = baseline.get("atr_14")
    long_term = long_term or {}

    extended = _is_extended(pct_change, rsi, ltp, high_20d)
    entry, entry_note = _propose_entry(ltp, atr, high_20d, long_term.get("supports"), extended)
    target, stop_loss, levels_basis, levels_method = _propose_target_stop(
        entry, atr, long_term, score, config.MIN_SIGNAL_SCORE)

    stop_loss = max(stop_loss, 0.01)  # never let a formula produce a non-positive price
    risk = entry - stop_loss
    reward = target - entry
    risk_reward = (reward / risk) if risk > 0 else None

    if config.MIN_RISK_REWARD_TO_ALERT is not None and \
            (risk_reward is None or risk_reward < config.MIN_RISK_REWARD_TO_ALERT):
        return None  # setup fires on the 5 checks, but the odds aren't good enough to bother with

    return {
        "symbol": symbol,
        "instrument": instrument,
        "close": round(ltp, 2),
        "pct_change": round(pct_change, 2),
        "score": score,
        "max_score": len(checks),
        "reasons": reasons,
        "date": datetime.now(_IST).strftime("%Y-%m-%d"),
        "extended": extended,
        "entry": round(entry, 2),
        "entry_note": entry_note,
        "target": round(target, 2),
        "stop_loss": round(stop_loss, 2),
        "levels_basis": levels_basis,
        "levels_method": levels_method,
        "risk_reward": round(risk_reward, 2) if risk_reward is not None else None,
    }
