from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import applications, automation, jobs, orchestration, profile, writing
        from job_agent.config import DOCS_DIR
        from job_agent.db import connect, init_db, row, rows, set_setting

        init_db()
        assert orchestration.pipeline_status()["policy"]["enabled"] is False

        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nalex@example.test\n212-555-0199\n"
            "Platform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.\n"
            "Automated release workflows used by five engineering teams.",
            encoding="utf-8",
        )
        assert profile.ingest_docs()["ingested"] == 1

        set_setting("pipeline_enabled", "true")
        set_setting("pipeline_min_score", "70")
        set_setting("pipeline_auto_write", "false")
        set_setting("pipeline_auto_apply", "false")
        set_setting("daily_application_limit", "2")
        first_job = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/platform",
                "description": "Build Python and TypeScript platform services.",
                "location": "Remote",
            }
        )
        low_score_job = jobs.add_manual_job(
            {
                "title": "Office Coordinator",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/office",
                "description": "Coordinate office schedules.",
                "location": "Remote",
            }
        )
        with connect() as conn:
            conn.execute("UPDATE jobs SET score = 95 WHERE id = ?", (first_job,))
            conn.execute("UPDATE jobs SET score = 20 WHERE id = ?", (low_score_job,))

        queued = orchestration.enqueue_eligible_jobs()
        assert queued == {"queued": 1, "eligible": 1, "daily_remaining": 1}, queued
        item = row("SELECT * FROM pipeline_items WHERE job_id = ?", (first_job,))
        assert item and item["status"] == "queued"
        assert orchestration.enqueue_eligible_jobs()["queued"] == 0

        drafted = orchestration.advance_item(int(item["id"]))
        assert drafted and drafted["stage"] == "package_ready"
        assert drafted["application_id"]
        reviewed = orchestration.advance_item(int(item["id"]))
        assert reviewed and reviewed["status"] == "review", reviewed
        assert reviewed["resume_compile_status"] == "compiled"
        assert len(rows("SELECT id FROM applications WHERE job_id = ?", (first_job,))) == 1

        applications.approve_application(int(reviewed["application_id"]))
        ready = orchestration.advance_item(int(item["id"]))
        assert ready and ready["status"] == "ready"
        assert ready["stage"] == "ready_to_apply"

        applications.mark_application_submitted(int(reviewed["application_id"]))
        completed = orchestration.advance_item(int(item["id"]))
        assert completed and completed["status"] == "submitted"
        assert completed["completed_at"]

        second_job = jobs.add_manual_job(
            {
                "title": "Backend Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/backend",
                "description": "Build Python APIs and SQL services.",
                "location": "Remote",
            }
        )
        with connect() as conn:
            conn.execute("UPDATE jobs SET score = 90 WHERE id = ?", (second_job,))
        second_enqueue = orchestration.enqueue_eligible_jobs()
        assert second_enqueue["queued"] == 1
        second_item = row("SELECT * FROM pipeline_items WHERE job_id = ?", (second_job,))
        assert second_item

        def fail_draft(_job_id: int, _mode: str | None) -> dict[str, object]:
            raise RuntimeError("fixture drafting failure")

        failed = orchestration.advance_item(int(second_item["id"]), draft_fn=fail_draft)
        assert failed and failed["status"] == "failed"
        assert "fixture drafting failure" in failed["last_error"]
        retried = orchestration.retry_item(int(second_item["id"]))
        assert retried["status"] == "queued" and retried["stage"] == "reconcile"
        recovered = orchestration.advance_item(int(second_item["id"]))
        assert recovered and recovered["stage"] == "package_ready"
        assert len(rows("SELECT id FROM applications WHERE job_id = ?", (second_job,))) == 1

        assert orchestration.enqueue_eligible_jobs()["daily_remaining"] == 0

        set_setting("daily_application_limit", "10")
        set_setting("pipeline_auto_write", "true")
        set_setting("pipeline_auto_apply", "true")
        third_job = jobs.add_manual_job(
            {
                "title": "Automation Engineer",
                "company": "ExampleCo",
                "url": "https://boards.greenhouse.io/example/jobs/automation-3",
                "description": "Build Python and TypeScript browser automation.",
                "location": "Remote",
            }
        )
        with connect() as conn:
            conn.execute("UPDATE jobs SET score = 98 WHERE id = ?", (third_job,))
        assert orchestration.enqueue_eligible_jobs()["queued"] == 1
        third_item = row("SELECT * FROM pipeline_items WHERE job_id = ?", (third_job,))
        assert third_item

        package = orchestration.advance_item(int(third_item["id"]))
        assert package and package["stage"] == "package_ready"
        writing_state = orchestration.advance_item(
            int(third_item["id"]),
            queue_writer_fn=lambda application_id: writing.queue_codex_draft(
                application_id,
                require_ready=False,
            ),
        )
        assert writing_state and writing_state["status"] == "writing"

        def fake_codex_runner(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            task_dir = Path(str(kwargs["cwd"]))
            request = json.loads((task_dir / "input.json").read_text(encoding="utf-8"))
            evidence = request["evidence"][0]
            evidence_id = evidence["id"]
            evidence_text = evidence["text"]
            result = {
                "resume": {
                    "headline": "Automation Engineer",
                    "summary": evidence_text,
                    "bullets": [{"text": evidence_text, "evidence_ids": [evidence_id]}],
                },
                "cover_letter": (
                    "Dear ExampleCo Hiring Team,\n\n"
                    "I am interested in the Automation Engineer role."
                ),
                "statements": [
                    {
                        "question": "Why are you interested in this role?",
                        "answer": "The role aligns with the responsibilities in the posting.",
                    },
                    {
                        "question": "Anything else you would like us to know?",
                        "answer": "My materials use verified source documents.",
                    },
                ],
                "email": {
                    "subject": "Interest in Automation Engineer",
                    "body": "Hi, I am applying for the Automation Engineer role at ExampleCo.",
                },
                "claims": [{"text": evidence_text, "evidence_ids": [evidence_id]}],
            }
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result), stderr="")

        written = writing.process_next_task(runner=fake_codex_runner)
        assert written and written["status"] == "completed", written
        review_ready = orchestration.advance_item(int(third_item["id"]))
        assert review_ready and review_ready["status"] == "review", review_ready

        application_id = int(review_ready["application_id"])
        applications.approve_application(application_id)
        browser_queued = orchestration.advance_item(int(third_item["id"]))
        assert browser_queued and browser_queued["status"] == "applying"
        browser_task = row(
            "SELECT * FROM application_tasks WHERE application_id = ? ORDER BY id DESC LIMIT 1",
            (application_id,),
        )
        assert browser_task and browser_task["status"] == "queued"

        final_review = automation.process_next_task(
            lambda _task, _app: {
                "status": "checkpoint",
                "checkpoint_kind": "final_review",
                "message": "Ready for final review.",
                "checkpoint": {},
            }
        )
        assert final_review and final_review["checkpoint_kind"] == "final_review"
        checkpoint = orchestration.advance_item(int(third_item["id"]))
        assert checkpoint and checkpoint["status"] == "checkpoint"

        automation.resolve_checkpoint(int(final_review["id"]), approve_submit=True)
        submitted_task = automation.process_next_task(
            lambda _task, _app: {
                "status": "submitted",
                "message": "Local fixture confirmed submission.",
                "result": {"confirmation_url": "https://example.test/thanks"},
            }
        )
        assert submitted_task and submitted_task["status"] == "submitted"
        submitted = orchestration.advance_item(int(third_item["id"]))
        assert submitted and submitted["status"] == "submitted"

        assert row("SELECT COUNT(*) AS count FROM pipeline_events")["count"] >= 20
        assert orchestration.pipeline_status()["total"] == 3

    print("pipeline workflow ok")


if __name__ == "__main__":
    main()
