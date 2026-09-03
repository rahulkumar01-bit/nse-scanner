"""
Thin wrapper around Kotak Neo's `neo_api_client` (the Kotak-Neo-api-v2 SDK).

Install with:
    pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"

Handles the two-step TOTP + MPIN login and exposes a couple of convenience
methods used by data_fetcher.py. If Kotak changes its SDK surface, this is
the only file that should need updates.
"""
import logging
import time

import pyotp

import config

log = logging.getLogger("nse_scanner.kotak_client")


class KotakClient:
    def __init__(self):
        self._client = None

    @staticmethod
    def _init_neo_api(NeoAPI):
        """
        Kotak's SDK has changed NeoAPI's constructor signature across
        releases — some versions require consumer_secret, newer ones reject
        it entirely. Try with it first (only if set), fall back without it.
        """
        base_kwargs = dict(
            environment=config.KOTAK_ENVIRONMENT,
            access_token=None,
            neo_fin_key=None,
            consumer_key=config.KOTAK_CONSUMER_KEY,
        )
        if config.KOTAK_CONSUMER_SECRET:
            try:
                return NeoAPI(consumer_secret=config.KOTAK_CONSUMER_SECRET, **base_kwargs)
            except TypeError:
                log.info("Installed neo_api_client rejects consumer_secret — retrying without it")
        return NeoAPI(**base_kwargs)

    @staticmethod
    def _call_totp_login(client, totp_code):
        """
        Kotak's own docs disagree with each other on this parameter's name —
        some show mobilenumber, others mobile_number, depending on SDK
        version/package. Try both rather than guess.
        """
        for kwarg_name in ("mobile_number", "mobilenumber"):
            try:
                return client.totp_login(
                    ucc=config.KOTAK_UCC,
                    totp=totp_code,
                    **{kwarg_name: config.KOTAK_MOBILE_NUMBER},
                )
            except TypeError:
                log.info("totp_login() rejected %s= — trying the other spelling", kwarg_name)
        raise RuntimeError(
            "totp_login() accepted neither mobile_number= nor mobilenumber= — "
            "check your installed neo_api_client version's signature"
        )

    def login(self):
        """Authenticate via TOTP + MPIN. Raises on failure."""
        from neo_api_client import NeoAPI

        for attr in ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE_NUMBER", "KOTAK_UCC",
                     "KOTAK_MPIN", "KOTAK_TOTP_SECRET"):
            if not getattr(config, attr):
                raise RuntimeError(f"{attr} is not set — check your .env file")
        client = self._init_neo_api(NeoAPI)

        totp_code = pyotp.TOTP(config.KOTAK_TOTP_SECRET).now()

        self._call_totp_login(client, totp_code)
        client.totp_validate(mpin=config.KOTAK_MPIN)

        self._client = client
        log.info("Logged in to Kotak Neo as %s", config.KOTAK_UCC)
        return self

    @property
    def client(self):
        if self._client is None:
            raise RuntimeError("Not logged in — call login() first")
        return self._client

    def ensure_session(self):
        """Re-login if the session looks dead. Call this before each scan cycle."""
        try:
            # A cheap call that requires a live session
            self._client.limits()
        except Exception:
            log.warning("Session appears stale, re-logging in")
            self.login()

    def historical_data(self, symbol, exchange_segment, instrument_token,
                         interval, from_date, to_date):
        """
        Wraps client.historical_data / client.get_historical_data — the exact
        method name has varied slightly across SDK releases, so try both.
        """
        for method_name in ("get_historical_data", "historical_data"):
            method = getattr(self._client, method_name, None)
            if method:
                return method(
                    exchange_segment=exchange_segment,
                    instrument_token=instrument_token,
                    interval=interval,
                    from_date=from_date,
                    to_date=to_date,
                )
        raise RuntimeError("SDK exposes neither get_historical_data nor historical_data — "
                            "check your installed neo_api_client version")

    def quotes(self, instrument_tokens):
        """Batch quote fetch for a list of {'instrument_token', 'exchange_segment'} dicts."""
        return self._client.quotes(instrument_tokens=instrument_tokens, quote_type="ltp")

    def scrip_master(self, exchange_segment="nse_cm"):
        """Downloads the instrument master (symbol -> token mapping)."""
        return self._client.scrip_master(exchange_segment=exchange_segment)
