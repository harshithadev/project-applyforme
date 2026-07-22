from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from .db import connect, log, now_iso, row, setting


def sent_today_count() -> int:
    found = row(
        """
        SELECT COUNT(*) AS count
        FROM events
        WHERE message LIKE 'Sent outreach email%'
          AND date(created_at) = date('now')
        """
    )
    return int(found["count"]) if found else 0


def can_send_email() -> tuple[bool, str]:
    limit = int(setting("daily_email_limit", "15") or "15")
    count = sent_today_count()
    if count >= limit:
        return False, f"Daily email limit reached ({count}/{limit})."
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        return False, f"Email sending is not configured. Missing: {', '.join(missing)}."
    return True, "Email can be sent."


def send_email(to_email: str, subject: str, body: str) -> dict[str, str]:
    allowed, reason = can_send_email()
    if not allowed:
        log(reason, "warning")
        return {"status": "blocked", "reason": reason}
    msg = EmailMessage()
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    log(f"Sent outreach email to {to_email}.")
    return {"status": "sent", "reason": "Email sent."}
