# NSE Momentum Scanner

Scans a configurable universe of NSE equities (and their current-month
futures) during market hours, and emails you when a stock shows a
composite of momentum/breakout signals — the kind of behaviour that
*sometimes* precedes a fast 5-10% move. It does **not** predict returns.

## ⚠️ Read this first

- **This is a heuristic technical screener, not a trading signal or investment advice.** No screener can reliably promise "~10% in 5 days" — that outcome depends on news, sentiment, and market conditions no historical-price scanner can see coming. Treat every alert as a starting point for your own research, not a signal to act on.
- Verify anything material (corporate actions, results, halts) before trading.
- If you later extend this to place orders automatically, that's algorithmic trading and may fall under SEBI's algo-trading framework — this project only reads data and sends email, it does not place any order.
- Data quality depends entirely on Kotak Neo's API; do your own sanity checks, especially around circuit filters, illiquid stocks, and F&O rollovers.

## How the screener works

**Data sources, combined:**
- **Yahoo Finance** supplies the historical daily baseline (20-day avg volume, 20-day high, RSI-14) as of the last *completed* trading day. Kotak's API doesn't expose historical candle data — confirmed against their own SDK docs, which list only live quotes, scrip master, and search under "Market Data," no historical/candle endpoint — so this fills that gap for free. Since this data doesn't change intraday, it's fetched once per calendar day and cached (`data/yf_cache.json`), not re-downloaded on every 10-minute cycle.
- **Kotak Neo** supplies today's *live* number (LTP, volume-so-far, open interest for futures) via `quotes()`, refreshed every scan cycle.

For each symbol, on each scan cycle, it combines yesterday's baseline with today's live snapshot and scores five independent checks:

1. **Day move** — today's LTP is up ≥ `DAY_CHANGE_PCT_THRESHOLD`% vs yesterday's close
2. **Volume surge** — today's volume-so-far ≥ `VOLUME_SURGE_MULTIPLE`× the 20-day average
3. **Breakout** — LTP above the `BREAKOUT_LOOKBACK_DAYS`-day high
4. **RSI momentum** — RSI(14) *as of yesterday's close* above `RSI_MOMENTUM_MIN` and rising vs the day before (an exact live-updated RSI isn't possible without today's final close)
5. **F&O long buildup** (futures only) — price up alongside rising open interest vs. the last time the scanner recorded it

A symbol is emailed once it scores at least `MIN_SIGNAL_SCORE` out of 5. All thresholds live in `config.py` — tune them to be stricter/looser. The same symbol won't be re-alerted for `DEDUPE_HOURS` hours.

**Note on futures:** since Kotak doesn't expose historical futures candles either, and stock futures track their underlying's cash price closely, the futures check reuses the *equity's* Yahoo Finance baseline (breakout/RSI/volume-avg) combined with the *futures* contract's live LTP and OI from Kotak. This is a reasonable approximation, not an exact futures-specific technical read.

## Setup

1. **Get Kotak Neo API access**: log in to Kotak Neo → Invest → Trade API → API Dashboard, generate your consumer key (a consumer secret is not required by the current SDK), and register for TOTP (you'll get a base32 secret to scan into an authenticator — save that raw secret, you need it in `.env`).

2. **Install dependencies**:
   ```bash
   cd nse-scanner
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
   ```

3. **Configure secrets**:
   ```bash
   cp .env.example .env
   # then edit .env with your Kotak credentials and email/app-password
   ```

4. **Edit your universe** (optional): `data/universe.csv` ships with ~100 liquid, F&O-eligible names. Add/remove symbols as you like — the scanner works fine with a shorter list too (faster cycles, fewer API calls).

5. **Test with a single scan**:
   ```bash
   python main.py --once
   ```
   Check `logs/scanner.log` for what happened. Fix any auth or symbol-mapping issues before leaving it running unattended.

6. **Run continuously**:
   ```bash
   python main.py
   ```
   It sleeps outside 09:15–15:30 IST on weekdays and scans every `SCAN_INTERVAL_MINUTES` (default 15) during market hours. Run it in `tmux`/`screen`, as a `systemd` service, or on a small always-on VM (a ₹200-300/month box is plenty) so it survives your laptop sleeping.

## Deploying to GitHub Actions (free, runs every 5 min)

Instead of a machine you keep on 24/7, `.github/workflows/scanner.yml` runs the scanner as a scheduled GitHub Actions job — genuinely free, no server to babysit.

**How it fits together:**
- The cron schedule fires every 5 minutes during 09:15–15:30 IST on weekdays (expressed in UTC, since Actions cron always runs in UTC).
- Each run is a brand-new disposable VM — there's no server between runs. `main.py --once` checks market hours itself and exits in seconds if triggered slightly outside the window (cron delays happen occasionally).
- Because there's no persistent disk, the dedupe file (`data/alert_state.json`) is committed back to the repo by the workflow after each run. This is the one part of the design that's specific to this deployment style — if you later move to a VPS, the plain local-file version in `state_store.py` works as-is.

**Setup:**

1. Push this project to a **public** GitHub repo (fine security-wise — your Kotak/SMTP credentials live in encrypted repo secrets, never in code; only the scanning logic itself is visible to others).

2. Add your secrets under **Settings → Secrets and variables → Actions → New repository secret**, one per line from `.env.example`: `KOTAK_CONSUMER_KEY`, `KOTAK_CONSUMER_SECRET`, `KOTAK_MOBILE_NUMBER`, `KOTAK_UCC`, `KOTAK_MPIN`, `KOTAK_TOTP_SECRET`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_FROM_EMAIL`, `ALERT_TO_EMAILS`.

3. Go to the **Actions** tab, enable workflows if prompted, then trigger **NSE Momentum Scanner → Run workflow** to do a manual test run. Check the run's logs the same way you'd check `logs/scanner.log` locally.

4. That's it — from the next weekday during market hours, it runs itself every 10 minutes.

**On minutes and interval:**

Public repos get **unlimited free GitHub Actions minutes**, so the scanner's default 10-minute cadence isn't there to stay under a cap — it's just a sensible default for a screener built on daily indicators (breakout, RSI, volume vs. 20-day average), where checking every 5 vs. 10 minutes makes little practical difference to signal quality but roughly doubles login/API calls to Kotak. If you want faster detection anyway, it costs you nothing on a public repo — just edit the `cron:` lines in `.github/workflows/scanner.yml` (swap the 10-minute pattern for the 5-minute one: `"*/5 4-9 * * 1-5"` for the main block, plus `"45,50,55 3 * * 1-5"` for the open).

## Deploying on your own Mac (launchd)

This runs the scanner continuously in the background on your Mac, restarting automatically if it crashes, and starting again on login — no cloud account needed.

**Setup:**

1. Do the normal local setup first (steps 2-4 above): create the venv, `pip install -r requirements.txt` plus the Kotak SDK, and fill in `.env`.

2. Install it as a background service:
   ```bash
   cd nse-scanner
   ./macos/install.sh
   ```
   This generates a `launchd` agent from `macos/com.nse-scanner.plist.template`, points it at your venv's Python, and loads it — it starts scanning immediately and will keep running (using `main.py`'s own market-hours loop, so it sleeps outside 09:15–15:30 IST and doesn't need a cron-style trigger every 5 minutes).

3. Check it's alive and watch the logs:
   ```bash
   launchctl list | grep nse-scanner
   tail -f logs/scanner.log
   ```

4. To stop it: `./macos/uninstall.sh`

**Things worth knowing about running this on a laptop specifically:**

- **Idle/system sleep is handled automatically.** The launch agent runs the scanner wrapped in `caffeinate -i -s`, which prevents idle and (on AC power) system sleep for as long as it's running — so as long as the lid stays open, it'll keep scanning without you needing to touch anything.
- **A closed lid with no external display is a hard limit.** macOS forces "clamshell sleep" at the hardware level when the lid closes and nothing else is attached — no background process, including this one, can override that. If you need lid-closed operation, either (a) keep it plugged into power with an external display, keyboard, or mouse connected (this puts the Mac in clamshell mode, where it stays awake lid-closed), or (b) just leave the lid open/use an external monitor during market hours, or (c) if neither is workable, the GitHub Actions or Render options from earlier don't have this problem at all since they don't depend on your laptop being awake.
- **Optional: auto-wake before market open.** If your Mac might be fully asleep overnight, `./macos/schedule-wake.sh` sets it to auto-wake at 09:05 IST on weekdays (`pmset`, needs sudo once). This only helps with regular sleep, not a closed lid — same caveat as above.

**Pausing and resuming:**

You don't need to uninstall/reinstall to turn this on and off — three small scripts handle that:
```bash
./macos/pause.sh    # stop it — stays off even across restarts, until you resume
./macos/resume.sh   # start it again
./macos/status.sh   # check whether it's currently running, plus recent log lines
```
`pause.sh` uses `launchctl unload -w`, which persists the off-state (a plain unload would silently restart at your next login/reboot — this won't).

## Notes on the Kotak Neo SDK

- The install command above pulls a specific tagged version (`v2.0.2`) of Kotak's `neo_api_client`. Kotak has occasionally renamed methods between releases (e.g. `historical_data` vs `get_historical_data`) — `kotak_client.py` tries both, but if Kotak ships a breaking change, that's the file to patch.
- `data_fetcher.build_token_map()` and `get_fno_token()` parse the scrip-master field names Kotak has documented (`pTrdSymbol`, `pSymbolName`, `pExpiryDate`, etc.). If Kotak changes these field names, print one raw row from `scrip_master()` and adjust the parsing there.
- Rate limits: Kotak's API supports up to 10 requests/second. Scanning ~100 symbols × 2 (equity + futures) well within a 15-minute cycle is comfortably inside that limit; if you widen the universe a lot, add a small `time.sleep()` between symbols.

## Extending it

- **Add option-chain signals** (unusual OI buildup in near-the-money strikes) — Kotak's API exposes option chain data; not included here to keep the first version focused.
- **Backtest before trusting it**: pull a few months of history and check how often score-≥3 signals were followed by a 10% move in 5 days for your universe, before relying on live alerts.
- **Swap email for Telegram/Slack** if you want faster notifications — the `notifier.py` module is intentionally small and easy to replace.
