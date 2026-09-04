import json
import os
from datetime import datetime, timedelta

import config


def _load():
    if not os.path.exists(config.STATE_FILE):
        return {}
    with open(config.STATE_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def should_alert(symbol, instrument):
    """True if we haven't alerted on this symbol+instrument within DEDUPE_HOURS."""
    state = _load()
    key = f"{symbol}:{instrument}"
    last = state.get(key)
    if not last:
        return True
    last_time = datetime.fromisoformat(last)
    return datetime.now() - last_time > timedelta(hours=config.DEDUPE_HOURS)


def mark_alerted(symbol, instrument):
    state = _load()
    key = f"{symbol}:{instrument}"
    state[key] = datetime.now().isoformat()
    _save(state)


HEARTBEAT_KEY = "__heartbeat_sent__"


def has_sent_heartbeat():
    """True once the one-time 'email delivery confirmed' message has been
    successfully sent. Used so it fires exactly once (ever), not once per
    day or once per no-signal cycle."""
    state = _load()
    return bool(state.get(HEARTBEAT_KEY))


def mark_heartbeat_sent():
    state = _load()
    state[HEARTBEAT_KEY] = datetime.now().isoformat()
    _save(state)


def get_previous_oi(oi_key):
    """Last open-interest value recorded for this key (typically
    "SYMBOL:EXPIRY", e.g. "RELIANCE:25SEP"), used to compute day-over-day
    OI change for the long-buildup check. Returns None if never recorded."""
    state = _load()
    return state.get(f"oi:{oi_key}")


def record_oi(oi_key, oi):
    state = _load()
    state[f"oi:{oi_key}"] = oi
    _save(state)
