from __future__ import annotations

import re
import threading
from typing import Callable

from . import emailer, writing
from .db import connect, log, now_iso, row, rows, setting


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_DELIVERY_ATTEMPTS = 3

_worker_started = False
_worker_lock = threading.Lock()
_worker_event = threading.Event()


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _worker_log(message: str, level: str = "info", meta: dict[str, object] | None = None) -> None:
    try:
        log(message, level, meta)
    except Exception:
        pass


def _company_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _validate_email(value: object) -> str:
    email = _clean(value).lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Enter a valid recipient email address")
    return email


def create_contact(value: object) -> dict[str, object]:
    payload = value if isinstance(value, dict) else {}
    company = _clean(payload.get("company"))
    if not company:
        raise ValueError("Contact company is required")
    email = _validate_email(payload.get("email"))
    existing = row("SELECT * FROM contacts WHERE lower(email) = lower(?)", (email,))
    if existing:
        raise ValueError(f"A contact with {email} already exists")
    now = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO contacts(
                company, name, role, email, source_url, confidence,
                verification_status, notes, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company,
                _clean(payload.get("name")),
                _clean(payload.get("role")),
                email,
                _clean(payload.get("source_url")),
                60,
                "manual",
                str(payload.get("notes") or "").strip(),
                now,
                now,
            ),
        )
        contact_id = int(cursor.lastrowid)
    log(f"Added outreach contact {email} at {company}.", meta={"contact_id": contact_id})
    return row("SELECT * FROM contacts WHERE id = ?", (contact_id,)) or {}


def list_contacts() -> list[dict[str, object]]:
    return rows("SELECT * FROM contacts ORDER BY company, name, email")


def _validate_message(subject: object, body: object) -> tuple[str, str]:
    clean_subject = _clean(subject)
    clean_body = str(body or "").strip()
    if not clean_subject:
        raise ValueError("Outreach subject is required")
    if len(clean_subject) > 200:
        raise ValueError("Outreach subject must be 200 characters or fewer")
    if not clean_body:
        raise ValueError("Outreach body is required")
    if len(clean_body) > 5_000:
        raise ValueError("Outreach body must be 5,000 characters or fewer")
    return clean_subject, clean_body


def _personalize_body(body: str, name: str) -> str:
    greeting = f"Hi {name.split()[0]}," if name else "Hello,"
    lines = body.splitlines()
    if lines and re.fullmatch(r"\s*(?:hi|hello)(?:\s+[^,]+)?,\s*", lines[0], re.IGNORECASE):
        lines[0] = greeting
        return "\n".join(lines)
    return f"{greeting}\n\n{body}"


def _thread_query(where: str, params: tuple[object, ...] = ()) -> dict[str, object] | None:
    return row(
        f"""
        SELECT outreach_threads.*, contacts.name AS contact_name, contacts.role AS contact_role,
               contacts.company AS contact_company, contacts.verification_status,
               jobs.title, jobs.company, applications.status AS application_status,
               applications.current_writing_version_id
        FROM outreach_threads
        JOIN contacts ON contacts.id = outreach_threads.contact_id
        JOIN applications ON applications.id = outreach_threads.application_id
        JOIN jobs ON jobs.id = applications.job_id
        WHERE {where}
        """,
        params,
    )


def _with_revisions(thread: dict[str, object]) -> dict[str, object]:
    result = dict(thread)
    revisions = rows(
        "SELECT * FROM outreach_revisions WHERE thread_id = ? ORDER BY version DESC",
        (int(thread["id"]),),
    )
    result["revisions"] = revisions
    active_id = thread.get("active_revision_id")
    result["active_revision"] = next(
        (revision for revision in revisions if int(revision["id"]) == int(active_id or 0)),
        None,
    )
    return result


def get_thread(thread_id: int) -> dict[str, object] | None:
    found = _thread_query("outreach_threads.id = ?", (thread_id,))
    return _with_revisions(found) if found else None


def list_threads() -> list[dict[str, object]]:
    found = rows(
        """
        SELECT outreach_threads.*, contacts.name AS contact_name, contacts.role AS contact_role,
               contacts.company AS contact_company, contacts.verification_status,
               jobs.title, jobs.company, applications.status AS application_status,
               applications.current_writing_version_id
        FROM outreach_threads
        JOIN contacts ON contacts.id = outreach_threads.contact_id
        JOIN applications ON applications.id = outreach_threads.application_id
        JOIN jobs ON jobs.id = applications.job_id
        ORDER BY outreach_threads.updated_at DESC, outreach_threads.id DESC
        """
    )
    return [_with_revisions(thread) for thread in found]


def _application_for_outreach(application_id: int) -> dict[str, object]:
    application = row(
        """
        SELECT applications.*, jobs.title, jobs.company
        FROM applications JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    if not application:
        raise ValueError(f"Application {application_id} does not exist")
    writing_version_id = application.get("current_writing_version_id")
    if not writing_version_id:
        raise ValueError("The application has no active writing version")
    version = writing.get_version(int(writing_version_id))
    if not version or version.get("validation", {}).get("status") == "failed":
        raise ValueError("The active writing version failed evidence validation")
    return application


def create_draft(application_id: int, contact_id: int) -> dict[str, object]:
    existing = _thread_query(
        "outreach_threads.application_id = ? AND outreach_threads.contact_id = ?",
        (application_id, contact_id),
    )
    if existing:
        return _with_revisions(existing)
    application = _application_for_outreach(application_id)
    contact = row("SELECT * FROM contacts WHERE id = ?", (contact_id,))
    if not contact:
        raise ValueError(f"Contact {contact_id} does not exist")
    if _company_key(contact["company"]) != _company_key(application["company"]):
        raise ValueError("The contact company does not match the application company")
    recipient = _validate_email(contact["email"])
    subject, body = _validate_message(
        application.get("email_subject"),
        _personalize_body(str(application.get("email_body") or ""), str(contact.get("name") or "")),
    )
    now = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO outreach_threads(
                application_id, contact_id, writing_version_id, status,
                recipient_email, idempotency_key, created_at, updated_at
            )
            VALUES(?, ?, ?, 'draft', ?, ?, ?, ?)
            """,
            (
                application_id,
                contact_id,
                int(application["current_writing_version_id"]),
                recipient,
                f"application:{application_id}:contact:{contact_id}",
                now,
                now,
            ),
        )
        thread_id = int(cursor.lastrowid)
        revision = conn.execute(
            """
            INSERT INTO outreach_revisions(thread_id, version, subject, body, created_at)
            VALUES(?, 1, ?, ?, ?)
            """,
            (thread_id, subject, body, now),
        )
        conn.execute(
            "UPDATE outreach_threads SET active_revision_id = ? WHERE id = ?",
            (int(revision.lastrowid), thread_id),
        )
    log(
        f"Drafted outreach to {recipient} for {application['title']} at {application['company']}.",
        meta={"thread_id": thread_id, "application_id": application_id, "contact_id": contact_id},
    )
    return get_thread(thread_id) or {}


def save_draft(thread_id: int, subject: object, body: object) -> dict[str, object]:
    thread = get_thread(thread_id)
    if not thread:
        raise ValueError(f"Outreach thread {thread_id} does not exist")
    if thread["status"] in {"queued", "sending", "sent"}:
        raise ValueError(f"A {thread['status']} outreach message cannot be edited")
    clean_subject, clean_body = _validate_message(subject, body)
    application = _application_for_outreach(int(thread["application_id"]))
    now = now_iso()
    with connect() as conn:
        found = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM outreach_revisions WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        revision = conn.execute(
            """
            INSERT INTO outreach_revisions(thread_id, version, subject, body, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (thread_id, int(found["next_version"]), clean_subject, clean_body, now),
        )
        conn.execute(
            """
            UPDATE outreach_threads
            SET writing_version_id = ?, active_revision_id = ?, approved_revision_id = NULL,
                status = 'draft', attempt_count = 0, last_error = '', approved_at = '',
                queued_at = '', updated_at = ?
            WHERE id = ?
            """,
            (
                int(application["current_writing_version_id"]),
                int(revision.lastrowid),
                now,
                thread_id,
            ),
        )
    log(f"Saved outreach revision for thread {thread_id}; approval is required again.")
    return get_thread(thread_id) or {}


def _assert_current_writing(thread: dict[str, object]) -> None:
    if int(thread["writing_version_id"]) != int(thread.get("current_writing_version_id") or 0):
        raise ValueError("The application writing changed; save a new outreach revision before sending")
    version = writing.get_version(int(thread["writing_version_id"]))
    if not version or version.get("validation", {}).get("status") == "failed":
        raise ValueError("The outreach message references an invalid writing version")


def approve(thread_id: int) -> dict[str, object]:
    thread = get_thread(thread_id)
    if not thread:
        raise ValueError(f"Outreach thread {thread_id} does not exist")
    if thread["status"] != "draft":
        raise ValueError("Only a draft outreach message can be approved")
    _assert_current_writing(thread)
    active_revision_id = int(thread.get("active_revision_id") or 0)
    if not active_revision_id:
        raise ValueError("The outreach thread has no active revision")
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE outreach_threads
            SET status = 'approved', approved_revision_id = ?, approved_at = ?,
                last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (active_revision_id, now, now, thread_id),
        )
    log(f"Approved outreach thread {thread_id}.", meta={"revision_id": active_revision_id})
    return get_thread(thread_id) or {}


def queue(thread_id: int) -> dict[str, object]:
    thread = get_thread(thread_id)
    if not thread:
        raise ValueError(f"Outreach thread {thread_id} does not exist")
    if thread["status"] in {"queued", "sending", "sent"}:
        return thread
    if thread["status"] not in {"draft", "approved", "failed", "uncertain"}:
        raise ValueError(f"Outreach thread cannot be queued from status {thread['status']}")
    _assert_current_writing(thread)
    active_revision_id = int(thread.get("active_revision_id") or 0)
    approved_revision_id = int(thread.get("approved_revision_id") or 0)
    approval_mode = setting("email_mode", "approval") == "approval"
    if approval_mode and approved_revision_id != active_revision_id:
        raise ValueError("Email approval mode requires approving the current outreach revision")
    if int(thread.get("attempt_count") or 0) >= MAX_DELIVERY_ATTEMPTS:
        raise ValueError(f"Outreach delivery stopped after {MAX_DELIVERY_ATTEMPTS} attempts")
    allowed, reason = emailer.can_send_email()
    if not allowed:
        raise ValueError(reason)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE outreach_threads
            SET status = 'queued', approved_revision_id = ?,
                approved_at = CASE WHEN approved_at = '' THEN ? ELSE approved_at END,
                queued_at = ?, last_error = '', updated_at = ?
            WHERE id = ?
            """,
            (active_revision_id, now, now, now, thread_id),
        )
    log(f"Queued approved outreach thread {thread_id} for delivery.")
    _worker_event.set()
    return get_thread(thread_id) or {}


def _claim_next_thread() -> dict[str, object] | None:
    with connect() as conn:
        found = conn.execute(
            "SELECT id FROM outreach_threads WHERE status = 'queued' ORDER BY queued_at, id LIMIT 1"
        ).fetchone()
        if not found:
            return None
        cursor = conn.execute(
            """
            UPDATE outreach_threads
            SET status = 'sending', attempt_count = attempt_count + 1, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now_iso(), int(found["id"])),
        )
        if not cursor.rowcount:
            return None
    return get_thread(int(found["id"]))


def process_next(
    deliverer: Callable[[str, str, str, str], dict[str, str]] | None = None,
) -> dict[str, object] | None:
    thread = _claim_next_thread()
    if not thread:
        return None
    thread_id = int(thread["id"])
    delivered = False
    try:
        _assert_current_writing(thread)
        revision = thread.get("active_revision")
        if not isinstance(revision, dict):
            raise RuntimeError("The queued outreach message has no active revision")
        if int(thread.get("approved_revision_id") or 0) != int(revision["id"]):
            raise RuntimeError("The queued outreach revision is not approved")
        send = deliverer or emailer.deliver_email
        result = send(
            str(thread["recipient_email"]),
            str(revision["subject"]),
            str(revision["body"]),
            str(thread["idempotency_key"]),
        )
        if result.get("status") != "sent":
            raise RuntimeError(str(result.get("reason") or "Email delivery was blocked"))
        delivered = True
        now = now_iso()
        with connect() as conn:
            conn.execute(
                """
                UPDATE outreach_threads
                SET status = 'sent', sent_at = ?, last_error = '', updated_at = ?
                WHERE id = ?
                """,
                (now, now, thread_id),
            )
        _worker_log(
            f"Completed outreach delivery to {thread['recipient_email']}.",
            meta={"thread_id": thread_id, "attempt": thread["attempt_count"]},
        )
    except Exception as exc:
        message = str(exc)
        failure_status = "uncertain" if delivered else "failed"
        with connect() as conn:
            conn.execute(
                "UPDATE outreach_threads SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (failure_status, message, now_iso(), thread_id),
            )
        _worker_log(
            f"Outreach delivery {failure_status} for {thread['recipient_email']}: {message}",
            "error",
            {"thread_id": thread_id, "attempt": thread["attempt_count"]},
        )
    return get_thread(thread_id)


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        with connect() as conn:
            conn.execute(
                """
                UPDATE outreach_threads
                SET status = 'uncertain', last_error = ?, updated_at = ?
                WHERE status = 'sending'
                """,
                (
                    "The local server stopped during delivery. Review before explicitly retrying.",
                    now_iso(),
                ),
            )
        _worker_started = True

        def loop() -> None:
            while True:
                try:
                    processed = process_next()
                except Exception as exc:
                    _worker_log(f"Outreach worker error: {exc}", "error")
                    processed = None
                if not processed:
                    _worker_event.wait(timeout=3)
                    _worker_event.clear()

        threading.Thread(target=loop, daemon=True, name="applyforme-outreach-worker").start()


def status() -> dict[str, object]:
    pending = row(
        "SELECT COUNT(*) AS count FROM outreach_threads WHERE status IN ('queued', 'sending')"
    )
    result = emailer.email_status()
    result["pending"] = int(pending["count"]) if pending else 0
    return result
