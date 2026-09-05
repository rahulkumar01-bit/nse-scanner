"""
Central configuration for the NSE scanner.
All secrets are read from environment variables (see .env.example) —
never hardcode credentials here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Kotak Neo API credentials (from Invest > Trade API > API Dashboard)
# ---------------------------------------------------------------------------
KOTAK_CONSUMER_KEY = os.getenv("KOTAK_CONSUMER_KEY")
KOTAK_CONSUMER_SECRET = os.getenv("KOTAK_CONSUMER_SECRET")  # optional — Kotak's newer SDK no longer requires this
KOTAK_MOBILE_NUMBER = os.getenv("KOTAK_MOBILE_NUMBER")   # with country code, e.g. +9198xxxxxxx
KOTAK_UCC = os.getenv("KOTAK_UCC")                        # Unique Client Code
KOTAK_MPIN = os.getenv("KOTAK_MPIN")
KOTAK_TOTP_SECRET = os.getenv("KOTAK_TOTP_SECRET")        # base32 secret from the authenticator QR setup
KOTAK_ENVIRONMENT = os.getenv("KOTAK_ENVIRONMENT", "prod")

# ---------------------------------------------------------------------------
# Email (SMTP) settings for alerts
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")          # use an app password, not your real password
ALERT_FROM_EMAIL = os.getenv("ALERT_FROM_EMAIL", SMTP_USER)
ALERT_TO_EMAILS = [e.strip() for e in os.getenv("ALERT_TO_EMAILS", "").split(",") if e.strip()]

# ---------------------------------------------------------------------------
# Universe of instruments to scan
# ---------------------------------------------------------------------------
# Path to a CSV with a single column "symbol" (NSE trading symbols, e.g. RELIANCE, TCS).
# A starter list of F&O-eligible large/mid caps is provided in data/universe.csv —
# edit it to widen or narrow what gets scanned.
UNIVERSE_FILE = os.path.join(os.path.dirname(__file__), "data", "universe.csv")

# Yahoo Finance baseline fetch resilience — Yahoo intermittently rate-limits
# rapid sequential requests, especially from shared cloud IPs (e.g. GitHub
# Actions runners). Fetches are batched into one call with retries, and a
# badly incomplete result is not cached so the next cycle retries.
YF_BATCH_RETRY_ATTEMPTS = 3
YF_BATCH_RETRY_BACKOFF_SEC = 15         # doubles-ish via *attempt multiplier
YF_MIN_COVERAGE_PCT = 50                # below this % of universe resolved, treat the fetch as failed

SCAN_EQUITIES = True
SCAN_FNO = True          # also evaluate futures for each underlying that has them — see FNO_MAX_EXPIRIES below

# ---------------------------------------------------------------------------
# Screener thresholds — this is a HEURISTIC technical scanner, not a
# predictive model. None of these thresholds "guarantee" a 10% move; they
# just flag stocks showing the kind of momentum that has sometimes preceded
# such moves. Tune freely.
# ---------------------------------------------------------------------------
MIN_PRICE = 30                 # ignore illiquid penny stocks below this price
MIN_AVG_TURNOVER_CR = 5        # min 20-day average daily turnover (INR crore) to consider a stock liquid enough

DAY_CHANGE_PCT_THRESHOLD = 4.0     # today's move vs prior close, in %
VOLUME_SURGE_MULTIPLE = 2.5        # today's volume vs 20-day average volume
BREAKOUT_LOOKBACK_DAYS = 20         # "N-day high" breakout lookback
RSI_PERIOD = 14
RSI_MOMENTUM_MIN = 60               # RSI should be above this and rising
MIN_SIGNAL_SCORE = 3                # out of the 5 checks in screener.py, how many must fire to alert

# F&O-specific (only used if quote data includes open interest)
OI_CHANGE_PCT_THRESHOLD = 8.0       # today's OI build-up, in %, for "long buildup" confirmation
FNO_MAX_EXPIRIES = 3                # scan the current + next N-1 monthly futures expiries, not just front-month

# ---------------------------------------------------------------------------
# Entry / target / stop-loss shown in each alert email. These are HEURISTIC
# technical levels derived from formulas below — not personalized advice.
# Position sizing and the final call are yours.
# ---------------------------------------------------------------------------
TARGET_RETURN_MIN_PCT = 3.0         # a true safety-net floor for weak-precedent cases only — NOT meant to be hit routinely.
                                     # (was 10.0 — backtesting showed this was acting as a de-facto ceiling: most
                                     # historical median returns fell below it, so almost every target just floored
                                     # here instead of reflecting the stock's real upside. See BREAKOUT_TARGET_PERCENTILE.)
TARGET_ATR_BASE_MULTIPLIER = 2.5    # target = entry + (this * ATR-14), scaled up further by signal strength below
TARGET_ATR_SCORE_STEP = 0.5         # each point of signal score above MIN_SIGNAL_SCORE adds this much to the ATR multiplier
ATR_PERIOD = 14
STOP_LOSS_ATR_MULTIPLIER = 1.5      # stop = entry - (this * ATR-14); wider ATR = more room, tighter = less
STOP_LOSS_PCT_FALLBACK = 4.0        # used only if ATR can't be computed (e.g. insufficient history)

# ---------------------------------------------------------------------------
# Long-term historical analysis (up to LONG_HISTORY_YEARS of daily data per
# symbol, weekly-cached). Used to (a) judge whether today's move already
# looks late-stage and propose a pullback entry instead of chasing the live
# price, and (b) ground target/stop-loss in this specific stock's own past
# behaviour rather than a generic ATR multiplier, where enough history and
# enough matching past setups exist. Falls back to the ATR-based method
# above when they don't (new listings, thin history).
# ---------------------------------------------------------------------------
LONG_HISTORY_YEARS = 15                  # yfinance returns whatever's actually available if the stock has traded less
LONG_TERM_CACHE_REFRESH_DAYS = 7         # re-download/re-derive weekly, not daily — this data barely moves day to day
LONG_TERM_ATR_PERIOD = 252               # ~1 trading year — a steadier read on "normal" volatility than ATR-14, which is itself often inflated mid-breakout

# Swing high/low (pivot) detection, used for support/resistance levels
PIVOT_WINDOW_DAYS = 10                   # a bar is a pivot if it's the highest/lowest within +/- this many trading days
PIVOT_CLUSTER_PCT = 2.0                  # merge pivot levels within this % of each other into one zone

# Per-symbol backtest: among all past days this stock had a signal-like setup
# (N-day breakout + RSI momentum, matching screener.py's own checks), what
# happened over the next HOLDING_PERIOD_DAYS trading days — deliberately a
# short window, matching how these alerts are actually meant to be traded
# (in and out within about a week, not held indefinitely).
HOLDING_PERIOD_DAYS = 6                  # ~5-7 trading days — the actual intended holding window
BREAKOUT_BACKTEST_FORWARD_DAYS = HOLDING_PERIOD_DAYS
BREAKOUT_BACKTEST_MIN_SAMPLES = 8        # below this many historical occurrences, don't trust the pattern stats — fall back to ATR
BREAKOUT_TARGET_PERCENTILE = 75          # use the 75th percentile (a strong-but-real historical outcome, achieved by
                                          # ~1 in 4 similar past setups) rather than the median, so the target reflects
                                          # this stock's actual best realistic short-term move, not a "typical" one.
                                          # Deliberately NOT the max — a single historical outlier isn't a reliable target.

# "Is the runup already extended?" — if any of these fire, don't recommend
# chasing the live price; propose a pullback entry instead.
RSI_EXTENDED_THRESHOLD = 78              # RSI-14 (yesterday's close) at/above this = already quite overbought
EXTENDED_DAY_MOVE_PCT = 8.0              # today's single-day move at/above this = risk of chasing a spike
EXTENDED_ABOVE_BREAKOUT_PCT = 5.0        # LTP already this much % above the 20-day high (not just freshly through it)

PULLBACK_ATR_MULTIPLIER = 1.0            # fallback pullback size (x ATR-14) when no historical support level is nearby
SUPPORT_SEARCH_MAX_PCT_BELOW = 15.0      # how far below price to look for a usable historical support (pullback entry / structural stop)
RESISTANCE_SEARCH_MAX_PCT_ABOVE = 20.0   # how far above entry to look for a resistance level that should cap the target
RESISTANCE_CAP_MIN_CAPTURE_PCT = 70      # a nearby resistance can only cap the (ATR-fallback) target if it still preserves
                                          # at least this % of the originally intended reward — otherwise a resistance
                                          # sitting just above entry would flatten a strong setup's target to almost nothing
SUPPORT_BUFFER_PCT = 1.0                 # place a structural stop this % below the raw support level, not exactly on it

# Stop-loss sizing is scaled to the actual holding period (sqrt-of-time
# volatility scaling — a 6-day hold's typical price range is roughly
# ATR * sqrt(6), not ATR itself, which is a single day's typical range) —
LONG_ATR_MAX_RATIO_TO_SHORT = 2.5        # cap the long-term (252d) daily ATR at this multiple of the current 14d ATR,
                                          # so a stock's sizing isn't dominated by an old volatility spike from years ago
STOP_LOSS_MIN_ATR_MULTIPLIER = 0.5       # bounds on the pattern/support-based stop distance, in units of the
STOP_LOSS_MAX_ATR_MULTIPLIER = 2.0       # holding-period-scaled ATR (was 3.0 — backtesting showed this produced oversized losses)

# ---------------------------------------------------------------------------
# Scan schedule
# ---------------------------------------------------------------------------
SCAN_INTERVAL_MINUTES = 15
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
TIMEZONE = "Asia/Kolkata"

# Avoid re-alerting on the same symbol+signal within this window
DEDUPE_HOURS = 6

STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "alert_state.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "scanner.log")
