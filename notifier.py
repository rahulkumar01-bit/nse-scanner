import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config

log = logging.getLogger("nse_scanner.notifier")

DISCLAIMER = (
    "This is an automated technical screener, not investment advice. Entry/target/"
    "stop-loss below are formula-derived heuristic levels (based on volatility and "
    "signal strength), not a personalized recommendation — they don't predict "
    "returns, and past patterns don't guarantee future moves. Verify independently, "
    "size your position appropriately, and manage your own risk before acting."
)


def _format_email(alerts):
    lines = [f"NSE Scanner — {len(alerts)} signal(s) found\n"]
    for a in alerts:
        lines.append(
            f"• {a['symbol']} ({a['instrument']})  ₹{a['close']}  "
            f"({a['pct_change']:+.2f}% today)  score {a['score']}/{a['max_score']}"
        )
        for r in a["reasons"]:
            lines.append(f"    - {r}")
        if a.get("entry") is not None:
            rr = f", R:R {a['risk_reward']}" if a.get("risk_reward") is not None else ""
            lines.append(
                f"    Entry ₹{a['entry']}  |  Target ₹{a['target']}  |  "
                f"Stop-loss ₹{a['stop_loss']}{rr}"
            )
        lines.append("")
    lines.append("-" * 60)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def send_alerts(alerts):
    if not alerts:
        return
    if not config.ALERT_TO_EMAILS:
        log.error("ALERT_TO_EMAILS is empty — cannot send email, printing instead:\n%s",
                   _format_email(alerts))
        return

    body = _format_email(alerts)
    msg = MIMEMultipart()
    msg["From"] = config.ALERT_FROM_EMAIL
    msg["To"] = ", ".join(config.ALERT_TO_EMAILS)
    msg["Subject"] = f"NSE Scanner: {len(alerts)} momentum signal(s) — {alerts[0]['date']}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.ALERT_FROM_EMAIL, config.ALERT_TO_EMAILS, msg.as_string())
        log.info("Sent alert email for %d signal(s)", len(alerts))
    except Exception:
        log.exception("Failed to send alert email")
