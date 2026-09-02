"""
Composite momentum/breakout screener.

IMPORTANT: this is a heuristic technical scanner. It flags stocks showing
the kind of price/volume behaviour that has SOMETIMES preceded fast moves —
it does not predict returns, and a flagged stock is not a recommendation to
trade. Position sizing, stop-losses, and your own judgement are on you.

Each candidate is scored 0-5 on independent checks. A symbol is alerted
when the score reaches config.MIN_SIGNAL_SCORE.
"""
import pandas as pd

import config


def evaluate(symbol, df, instrument="EQ", oi_change_pct=None):
    """
    df: DataFrame from data_fetcher.compute_indicators(), most recent row = today.
    Returns None if no signal, else a dict describing the alert.
    """
    if df.empty or len(df) < config.BREAKOUT_LOOKBACK_DAYS + 2:
        return None

    today = df.iloc[-1]
    prev = df.iloc[-2]

    if any(pd.isna(today.get(c)) for c in
           ("close", "volume", "avg_volume_20d", "high_20d", "rsi_14", "avg_turnover_cr_20d")):
        return None

    if today["close"] < config.MIN_PRICE:
        return None
    if today["avg_turnover_cr_20d"] < config.MIN_AVG_TURNOVER_CR:
        return None

    checks = {}

    # 1. Sharp single-day move
    checks["day_move"] = today["pct_change"] >= config.DAY_CHANGE_PCT_THRESHOLD

    # 2. Volume surge vs 20-day average
    checks["volume_surge"] = today["volume_ratio"] >= config.VOLUME_SURGE_MULTIPLE

    # 3. Breakout above N-day high
    checks["breakout"] = today["close"] > today["high_20d"]

    # 4. RSI momentum: above threshold and rising vs yesterday
    checks["rsi_momentum"] = (
        today["rsi_14"] >= config.RSI_MOMENTUM_MIN and today["rsi_14"] > prev.get("rsi_14", 0)
    )

    # 5. Optional F&O confirmation: long buildup (price up + OI up)
    if oi_change_pct is not None:
        checks["oi_buildup"] = oi_change_pct >= config.OI_CHANGE_PCT_THRESHOLD and checks["day_move"]
    else:
        checks["oi_buildup"] = False

    score = sum(1 for v in checks.values() if v)
    if score < config.MIN_SIGNAL_SCORE:
        return None

    reasons = []
    if checks["day_move"]:
        reasons.append(f"up {today['pct_change']:.1f}% today")
    if checks["volume_surge"]:
        reasons.append(f"volume {today['volume_ratio']:.1f}x the 20-day average")
    if checks["breakout"]:
        reasons.append(f"broke above its {config.BREAKOUT_LOOKBACK_DAYS}-day high")
    if checks["rsi_momentum"]:
        reasons.append(f"RSI(14) at {today['rsi_14']:.0f} and rising")
    if checks["oi_buildup"]:
        reasons.append(f"open interest up {oi_change_pct:.1f}% (long buildup)")

    return {
        "symbol": symbol,
        "instrument": instrument,
        "close": round(float(today["close"]), 2),
        "pct_change": round(float(today["pct_change"]), 2),
        "score": score,
        "max_score": len(checks),
        "reasons": reasons,
        "date": today["date"],
    }
