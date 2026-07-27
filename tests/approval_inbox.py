from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import approvals, jobs, outreach
        from job_agent.config import DOCS_DIR
        from job_agent.db import connect, init_db, now_iso, row, rows, set_setting

        init_db()
        job_id = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/platform-engineer",
                "description": "Build Python services and reliable automation.",
                "location": "Remote",
            }
        )
        now = now_iso()
        with connect() as conn:
            application_id = int(
                conn.execute(
                    """
                    INSERT INTO applications(
                      job_id, mode, status, resume_compile_status,
                      writing_status, created_at, updated_at
                    )
                    VALUES(?, 'review', 'drafted', 'compiled', 'draft', ?, ?)
                    """,
                    (job_id, now, now),
                ).lastrowid
            )
            version_id = int(
                conn.execute(
                    """
                    INSERT INTO writing_versions(
                      application_id, version, origin, status, content_json,
                      evidence_json, validation_json, created_at
                    )
                    VALUES(?, 1, 'test', 'draft', ?, '[]', ?, ?)
                    """,
                    (
                        application_id,
                        json.dumps(
                            {
                                "resume": {"summary": "Platform engineer", "bullets": []},
                                "cover_letter": "Hello hiring team.",
                                "statements": [],
                                "email": {"subject": "Platform Engineer", "body": "Hello."},
                            }
                        ),
                        json.dumps({"status": "passed"}),
                        now,
                    ),
                ).lastrowid
            )
            conn.execute(
                """
                UPDATE applications
                SET current_writing_version_id = ?
                WHERE id = ?
                """,
                (version_id, application_id),
            )

        first_sync = approvals.sync_inbox()
        assert first_sync["created"] == 1
        package_item = approvals.list_items()[0]
        assert package_item["kind"] == "application_review"
        assert {item["id"] for item in package_item["actions"]} == {"approve", "skip"}

        notifications: list[tuple[str, str]] = []

        def notify(title: str, message: str) -> None:
            notifications.append((title, message))

        delivered = approvals.send_pending_notifications(
            notifier=notify,
            current_time=datetime(2026, 1, 1, 12, 0),
        )
        duplicate = approvals.send_pending_notifications(
            notifier=notify,
            current_time=datetime(2026, 1, 1, 12, 0),
        )
        assert delivered["sent"] == 1
        assert duplicate["sent"] == 0
        assert len(notifications) == 1

        approved = approvals.resolve_item(
            int(package_item["id"]),
            "approve",
            note="Resume and statements reviewed.",
        )
        assert approved["result"]["status"] == "approved"
        assert row("SELECT status FROM applications WHERE id = ?", (application_id,))["status"] == "approved"

        checkpoint_time = "2026-01-02T12:00:00+00:00"
        with connect() as conn:
            task_id = int(
                conn.execute(
                    """
                    INSERT INTO application_tasks(
                      application_id, adapter, target_url, mode, status,
                      current_step, message, checkpoint_kind, checkpoint_json,
                      created_at, updated_at
                    )
                    VALUES(
                      ?, 'greenhouse', 'https://example.test/apply', 'review',
                      'checkpoint', 'checkpoint', ?, 'unknown_field', ?, ?, ?
                    )
                    """,
                    (
                        application_id,
                        "A required field needs an answer.",
                        json.dumps(
                            {
                                "fields": [
                                    {
                                        "question": "Why are you interested?",
                                        "type": "textarea",
                                        "options": [],
                                    }
                                ]
                            }
                        ),
                        checkpoint_time,
                        checkpoint_time,
                    ),
                ).lastrowid
            )
        approvals.sync_inbox()
        browser_item = next(
            item for item in approvals.list_items() if item["kind"] == "browser_checkpoint"
        )
        try:
            approvals.resolve_item(int(browser_item["id"]), "continue", {"answers": {}})
            raise AssertionError("Browser checkpoint continued without its required answer")
        except ValueError as exc:
            assert "Answer every required field" in str(exc)
        continued = approvals.resolve_item(
            int(browser_item["id"]),
            "continue",
            {"answers": {"Why are you interested?": "The role matches my platform work."}},
        )
        assert continued["result"]["status"] == "queued"
        assert row("SELECT status FROM application_tasks WHERE id = ?", (task_id,))["status"] == "queued"
        assert row(
            "SELECT answer FROM answer_rules WHERE question = ?",
            ("Why are you interested?",),
        )["answer"] == "The role matches my platform work."

        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        document_path = DOCS_DIR / "new-evidence.txt"
        document_content = b"Reviewed Python platform evidence."
        document_path.write_bytes(document_content)
        document_time = "2026-01-02T13:00:00+00:00"
        with connect() as conn:
            document_id = int(
                conn.execute(
                    """
                    INSERT INTO documents(
                      path, name, kind, content, summary, sha256, extractor,
                      ingest_status, review_status, size_bytes, metadata,
                      created_at, updated_at
                    )
                    VALUES(
                      ?, 'new-evidence.txt', 'source', ?, ?, ?, 'utf-8-text',
                      'pending_review', 'pending', ?, '{}', ?, ?
                    )
                    """,
                    (
                        str(document_path.resolve()),
                        document_content.decode("utf-8"),
                        document_content.decode("utf-8"),
                        hashlib.sha256(document_content).hexdigest(),
                        len(document_content),
                        document_time,
                        document_time,
                    ),
                ).lastrowid
            )
        approvals.sync_inbox()
        document_item = next(
            item
            for item in approvals.list_items()
            if item["source_type"] == "document"
            and int(item["source_id"]) == document_id
        )
        document_result = approvals.resolve_item(
            int(document_item["id"]),
            "approve",
        )
        assert document_result["result"]["status"] == "ready"

        contact = outreach.create_contact(
            {
                "company": "ExampleCo",
                "name": "Hiring Manager",
                "email": "manager@example.test",
            }
        )
        outreach_time = "2026-01-03T12:00:00+00:00"
        with connect() as conn:
            thread_id = int(
                conn.execute(
                    """
                    INSERT INTO outreach_threads(
                      application_id, contact_id, writing_version_id, status,
                      recipient_email, idempotency_key, created_at, updated_at
                    )
                    VALUES(?, ?, ?, 'draft', ?, ?, ?, ?)
                    """,
                    (
                        application_id,
                        int(contact["id"]),
                        version_id,
                        contact["email"],
                        f"approval-test:{application_id}:{contact['id']}",
                        outreach_time,
                        outreach_time,
                    ),
                ).lastrowid
            )
            revision_id = int(
                conn.execute(
                    """
                    INSERT INTO outreach_revisions(
                      thread_id, version, subject, body, created_at
                    )
                    VALUES(?, 1, 'Platform Engineer', 'Hello Hiring Manager.', ?)
                    """,
                    (thread_id, outreach_time),
                ).lastrowid
            )
            conn.execute(
                "UPDATE outreach_threads SET active_revision_id = ? WHERE id = ?",
                (revision_id, thread_id),
            )
        approvals.sync_inbox()
        outreach_item = next(
            item for item in approvals.list_items() if item["kind"] == "outreach_review"
        )
        outreach_result = approvals.resolve_item(int(outreach_item["id"]), "approve")
        assert outreach_result["result"]["status"] == "approved"

        pipeline_time = "2026-01-04T12:00:00+00:00"
        with connect() as conn:
            pipeline_id = int(
                conn.execute(
                    """
                    INSERT INTO pipeline_items(
                      job_id, application_id, status, stage, message,
                      last_error, created_at, updated_at
                    )
                    VALUES(?, ?, 'failed', 'browser', ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        application_id,
                        "Browser automation failed.",
                        "Fixture failure",
                        pipeline_time,
                        pipeline_time,
                    ),
                ).lastrowid
            )
        approvals.sync_inbox()
        pipeline_item = next(
            item for item in approvals.list_items() if item["kind"] == "pipeline_attention"
        )
        retried = approvals.resolve_item(int(pipeline_item["id"]), "retry")
        assert retried["result"]["status"] == "queued"

        decision_actions = {item["action"] for item in approvals.decision_history()}
        assert {"approve", "continue", "retry"} <= decision_actions
        assert len(rows("SELECT id FROM approval_decisions")) == 5

        with connect() as conn:
            conn.execute(
                """
                UPDATE pipeline_items
                SET status = 'failed', stage = 'browser', message = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "Browser automation failed again.",
                    "Second fixture failure",
                    "2099-01-01T00:00:00+00:00",
                    pipeline_id,
                ),
            )
        approvals.sync_inbox()
        set_setting("notification_quiet_start", "22:00")
        set_setting("notification_quiet_end", "08:00")
        quiet = approvals.send_pending_notifications(
            notifier=notify,
            current_time=datetime(2026, 1, 1, 23, 0),
        )
        assert quiet["status"] == "quiet"
        assert len(notifications) == 1
        assert approvals.notification_status(datetime(2026, 1, 1, 23, 0))["quiet"]

    print("approval inbox ok")


if __name__ == "__main__":
    main()
