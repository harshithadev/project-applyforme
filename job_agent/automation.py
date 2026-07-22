from __future__ import annotations

import shutil

from .db import log, row, setting


def playwright_available() -> bool:
    return shutil.which("playwright") is not None


def submitted_today_count() -> int:
    found = row(
        """
        SELECT COUNT(*) AS count
        FROM applications
        WHERE status = 'submitted'
          AND date(updated_at) = date('now')
        """
    )
    return int(found["count"]) if found else 0


def automation_status() -> dict[str, object]:
    return {
        "playwright_available": playwright_available(),
        "mode": setting("mode", "review"),
        "submitted_today": submitted_today_count(),
    }


def apply_application(application_id: int) -> dict[str, str]:
    app = row(
        """
        SELECT applications.*, jobs.url, jobs.title, jobs.company
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    if not app:
        return {"status": "error", "message": "Application does not exist."}
    mode = setting("mode", "review")
    if mode == "review" and app["status"] != "approved":
        message = "Review mode requires approval before browser submission."
        log(message, "warning", {"application_id": application_id})
        return {"status": "blocked", "message": message}
    limit = int(setting("daily_application_limit", "10") or "10")
    count = submitted_today_count()
    if count >= limit:
        message = f"Daily application limit reached ({count}/{limit})."
        log(message, "warning", {"application_id": application_id})
        return {"status": "blocked", "message": message}
    if not playwright_available():
        message = "Playwright is not installed, so browser submission is queued for manual/Codex operation."
        log(message, "warning", {"application_id": application_id, "url": app["url"]})
        return {"status": "queued", "message": message}
    message = "Playwright is available. Implement site-specific adapters before live submission."
    log(message, "warning", {"application_id": application_id, "url": app["url"]})
    return {"status": "blocked", "message": message}
