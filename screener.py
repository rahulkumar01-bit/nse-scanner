"""
Composite momentum/breakout screener.

IMPORTANT: this is a heuristic technical scanner. It flags stocks showing
the kind of price/volume behaviour that has SOMETIMES preceded fast moves —
it does not predict returns, and a flagged stock is not a recommendation to
trade. Position sizing, stop-losses, and your own judgement are on you.

Each candidate is scored 0-5 on independent checks, combining yesterday's
baseline stats (from Yahoo Finance) with today's live snapshot (from
Kotak). A symbol clearing config.MIN_SIGNAL_SCORE then gets a full
entry/target/stop-loss workup:
  - Entry: current price, unless the stock looks extended (RSI/distance from
    its 50-day average), in which case a pullback level is proposed instead.
  - Stop-loss: the actual recent swing low (real support), capped so it's
    never unreasonably wide.
  - Target: the nearest real prior resistance level (52-week/3y/5y/all-time
    high) that clears your minimum expectation, or a volatility+strength
    projection if no such level exists.
A result is only "favorable" (and gets emailed) if the resulting
risk:reward clears config.MIN_RISK_REWARD_TO_ALERT — otherwise it's
computed and logged, but no email is sent.
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

    # --- Entry: is the current price a reasonable entry, or already extended? ---
    rsi_extended = rsi is not None and rsi >= config.EXTENDED_RSI_THRESHOLD
    sma_50 = baseline.get("sma_50")
    pct_above_sma50 = ((ltp - sma_50) / sma_50 * 100) if sma_50 else None
    ma_extended = pct_above_sma50 is not None and pct_above_sma50 >= config.EXTENDED_MA_DISTANCE_PCT
    is_extended = rsi_extended or ma_extended

    ema_20 = baseline.get("ema_20")
    if is_extended and ema_20 and ema_20 < ltp:
        entry = ema_20
        extended_bits = []
        if rsi_extended:
            extended_bits.append(f"RSI {rsi:.0f}")
        if ma_extended:
            extended_bits.append(f"{pct_above_sma50:.0f}% above 50-day average")
        entry_note = (f"stock looks extended ({', '.join(extended_bits)}) — "
                      f"proposed entry is a pullback to the 20-day EMA (₹{entry:.2f}), "
                      f"not today's price (₹{ltp:.2f})")
    else:
        entry = ltp
        entry_note = "current market price"

    # --- Stop-loss: anchored to the actual recent swing low, capped by ATR ---
    atr = baseline.get("atr_14")
    swing_low = baseline.get("swing_low")
    stop_candidates = []
    if swing_low and swing_low < entry:
        stop_candidates.append(("recent swing low", swing_low * (1 - config.SWING_LOW_BUFFER_PCT / 100)))
    if atr:
        stop_candidates.append(("ATR-based cap", entry - config.STOP_LOSS_MAX_ATR_MULTIPLIER * atr))
    if not stop_candidates:
        stop_loss = entry * (1 - config.STOP_LOSS_PCT_FALLBACK / 100)
        stop_basis = "flat % fallback (insufficient history for ATR/swing low)"
    elif len(stop_candidates) == 1:
        stop_basis, stop_loss = stop_candidates[0]
    else:
        # Use the swing low, but never let it be wider than the ATR-based cap
        (_, swing_stop), (_, atr_cap) = stop_candidates
        if swing_stop >= atr_cap:
            stop_loss, stop_basis = swing_stop, "recent swing low"
        else:
            stop_loss, stop_basis = atr_cap, f"ATR cap (swing low was wider than {config.STOP_LOSS_MAX_ATR_MULTIPLIER}x ATR away)"
    stop_loss = max(stop_loss, 0.01)  # never let logic produce a non-positive price

    # --- Target: prefer real prior resistance over a formula, when one qualifies ---
    target_from_min_pct = entry * (1 + config.TARGET_RETURN_MIN_PCT / 100)
    resistance_candidates = [
        lvl for lvl in (baseline.get("high_52w"), baseline.get("high_3y"),
                         baseline.get("high_5y"), baseline.get("all_time_high"))
        if lvl and lvl >= target_from_min_pct
    ]
    if resistance_candidates:
        target = min(resistance_candidates)  # nearest qualifying resistance above entry
        target_basis = "prior historical resistance"
    elif atr:
        atr_multiplier = config.TARGET_ATR_BASE_MULTIPLIER + \
            max(0, score - config.MIN_SIGNAL_SCORE) * config.TARGET_ATR_SCORE_STEP
        target = max(entry + atr_multiplier * atr, target_from_min_pct)
        target_basis = "volatility/momentum projection (no qualifying resistance level found)"
    else:
        target = target_from_min_pct
        target_basis = "minimum-expectation floor (insufficient data for ATR or resistance)"

    risk = entry - stop_loss
    reward = target - entry
    risk_reward = (reward / risk) if risk > 0 else None
    favorable = risk_reward is not None and risk_reward >= config.MIN_RISK_REWARD_TO_ALERT

    return {
        "symbol": symbol,
        "instrument": instrument,
        "close": round(ltp, 2),
        "pct_change": round(pct_change, 2),
        "score": score,
        "max_score": len(checks),
        "reasons": reasons,
        "date": datetime.now(_IST).strftime("%Y-%m-%d"),
        "entry": round(entry, 2),
        "entry_note": entry_note,
        "target": round(target, 2),
        "target_basis": target_basis,
        "stop_loss": round(stop_loss, 2),
        "stop_basis": stop_basis,
        "risk_reward": round(risk_reward, 2) if risk_reward is not None else None,
        "favorable": favorable,
    }
