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
# technical levels derived from the formulas/logic below — not personalized
# advice. Position sizing and the final call are yours.
# ---------------------------------------------------------------------------
LONG_HISTORY_YEARS = 15             # how far back to pull daily history for long-term levels (52wk/3y/5y/all-time highs)

# Entry: is the stock already extended (late-stage move), or a reasonable entry now?
EXTENDED_RSI_THRESHOLD = 70.0        # RSI(14) at/above this = overbought
EXTENDED_MA_DISTANCE_PCT = 12.0      # price this much above its 50-day SMA = stretched
PULLBACK_EMA_PERIOD = 20             # when extended, proposed entry = this EMA instead of chasing current price

# Stop-loss: anchored to the actual recent swing low (real support), not just a formula
SWING_LOW_LOOKBACK_DAYS = 20
SWING_LOW_BUFFER_PCT = 1.0           # stop sits this much below the swing low, not exactly on it
STOP_LOSS_ATR_MULTIPLIER = 1.5       # fallback/cap basis: stop = entry - (this * ATR-14)
STOP_LOSS_MAX_ATR_MULTIPLIER = 3.0   # never let the swing-low stop be wider than this many ATRs from entry
STOP_LOSS_PCT_FALLBACK = 4.0         # used only if ATR AND swing low are both unavailable
ATR_PERIOD = 14

# Target: prefer real prior resistance (52wk/3y/5y/all-time high) over a formula, when
# one exists above entry and clears your minimum — else fall back to a volatility+
# signal-strength projection, same idea as before.
TARGET_RETURN_MIN_PCT = 10.0        # floor: target is never below entry * (1 + this/100) — your minimum expectation
TARGET_ATR_BASE_MULTIPLIER = 2.5    # fallback formula: entry + (this * ATR-14), scaled up further by signal strength
TARGET_ATR_SCORE_STEP = 0.5         # each point of signal score above MIN_SIGNAL_SCORE adds this much to the ATR multiplier

# Only actually email when the resulting risk:reward clears this bar — otherwise the
# setup is logged (visible in the Actions run log) but no email is sent.
MIN_RISK_REWARD_TO_ALERT = 1.5

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
