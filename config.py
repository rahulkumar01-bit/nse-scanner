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
FNO_MAX_EXPIRIES = 2                # scan the current + next N-1 monthly futures expiries, not just front-month

# ---------------------------------------------------------------------------
# Entry / target / stop-loss shown in each alert email. These are HEURISTIC
# technical levels derived from formulas below — not personalized advice.
# Position sizing and the final call are yours.
# ---------------------------------------------------------------------------
TARGET_RETURN_MIN_PCT = 10.0        # floor: target is never below entry * (1 + this/100) — your minimum expectation
TARGET_ATR_BASE_MULTIPLIER = 2.5    # target = entry + (this * ATR-14), scaled up further by signal strength below
TARGET_ATR_SCORE_STEP = 0.5         # each point of signal score above MIN_SIGNAL_SCORE adds this much to the ATR multiplier
ATR_PERIOD = 14
STOP_LOSS_ATR_MULTIPLIER = 1.5      # stop = entry - (this * ATR-14); wider ATR = more room, tighter = less
STOP_LOSS_PCT_FALLBACK = 4.0        # used only if ATR can't be computed (e.g. insufficient history)

# ---------------------------------------------------------------------------
# Scan schedule
# ---------------------------------------------------------------------------
SCAN_INTERVAL_MINUTES = 15
MARKET_OPEN = "08:45"
MARKET_CLOSE = "15:30"
TIMEZONE = "Asia/Kolkata"

# Avoid re-alerting on the same symbol+signal within this window
DEDUPE_HOURS = 6

STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "alert_state.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "logs", "scanner.log")
