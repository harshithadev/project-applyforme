from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from hashlib import sha256
from typing import Callable

from .db import log, row, setting


SMTPConnector = Callable[..., object]


def smtp_configuration() -> dict[str, object]:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM"]
    missing = [key for key in required if not os.getenv(key)]
    return {
        "configured": not missing,
        "missing": missing,
        "host": os.getenv("SMTP_HOST", ""),
        "port": os.getenv("SMTP_PORT", ""),
        "user": os.getenv("SMTP_USER", ""),
        "from": os.getenv("EMAIL_FROM", ""),
    }


def sent_today_count() -> int:
    found = row(
        """
        SELECT
          (SELECT COUNT(*) FROM outreach_threads
           WHERE status IN ('sent', 'uncertain')
             AND date(CASE WHEN sent_at = '' THEN updated_at ELSE sent_at END) = date('now'))
          +
          (SELECT COUNT(*) FROM events
           WHERE (message LIKE 'Sent outreach email%' OR message LIKE 'Sent direct outreach email%')
             AND date(created_at) = date('now')) AS count
        """
    )
    return int(found["count"]) if found else 0


def can_send_email() -> tuple[bool, str]:
    limit = int(setting("daily_email_limit", "15") or "15")
    count = sent_today_count()
    if count >= limit:
        return False, f"Daily email limit reached ({count}/{limit})."
    missing = smtp_configuration()["missing"]
    if missing:
        return False, f"Email sending is not configured. Missing: {', '.join(missing)}."
    return True, "Email can be sent."


def email_status() -> dict[str, object]:
    allowed, reason = can_send_email()
    limit = int(setting("daily_email_limit", "15") or "15")
    return {
        "configured": bool(smtp_configuration()["configured"]),
        "available": allowed,
        "mode": setting("email_mode", "approval"),
        "sent_today": sent_today_count(),
        "daily_limit": limit,
        "message": reason,
    }


def verify_smtp_connection(
    connector: SMTPConnector = smtplib.SMTP,
) -> dict[str, str]:
    configuration = smtp_configuration()
    missing = configuration["missing"]
    if missing:
        raise ValueError(f"Email sending is not configured. Missing: {', '.join(missing)}.")
    try:
        port = int(str(configuration["port"]))
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be a valid integer") from exc
    try:
        with connector(str(configuration["host"]), port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(
                os.environ["SMTP_USER"],
                os.environ["SMTP_PASSWORD"],
            )
            if hasattr(smtp, "noop"):
                smtp.noop()
    except Exception as exc:
        raise RuntimeError(f"SMTP verification failed: {exc}") from exc
    return {
        "status": "verified",
        "message": "SMTP connection and login verified without sending an email.",
    }


def deliver_email(
    to_email: str,
    subject: str,
    body: str,
    idempotency_key: str = "",
) -> dict[str, str]:
    allowed, reason = can_send_email()
    if not allowed:
        log(reason, "warning")
        return {"status": "blocked", "reason": reason}
    msg = EmailMessage()
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = to_email
    msg["Subject"] = subject
    if idempotency_key:
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        msg["Message-ID"] = f"<applyforme-{digest}@local>"
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return {"status": "sent", "reason": "Email sent."}


def send_email(to_email: str, subject: str, body: str) -> dict[str, str]:
    if setting("email_mode", "approval") == "approval":
        reason = "Email approval mode requires an approved outreach draft before sending."
        log(reason, "warning")
        return {"status": "blocked", "reason": reason}
    result = deliver_email(to_email, subject, body)
    if result["status"] == "sent":
        log(f"Sent direct outreach email to {to_email}.")
    return result
