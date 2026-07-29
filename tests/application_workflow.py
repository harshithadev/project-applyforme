from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import applications, automation, jobs, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import connect, init_db, row

        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nalex@example.test\n212-555-0199\n"
            "Platform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        profile.ingest_docs()
        job_id = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://boards.greenhouse.io/example/jobs/123",
                "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
            }
        )
        app = applications.draft_application(job_id)
        if app["resume_compile_status"] != "compiled":
            pdf_path = Path(str(app["resume_tex_path"])).with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-1.4\n% local workflow fixture\n")
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE applications
                    SET resume_compile_status = 'compiled', resume_pdf_path = ?,
                        resume_pdf_bytes = ?, resume_pdf_pages = 1
                    WHERE id = ?
                    """,
                    (str(pdf_path), pdf_path.stat().st_size, int(app["id"])),
                )

        blocked = automation.apply_application(int(app["id"]))
        assert blocked["status"] == "blocked"
        applications.approve_application(int(app["id"]))
        queued = automation.apply_application(int(app["id"]))
        assert queued["status"] == "queued"
        assert queued["adapter"] == "greenhouse"
        assert queued["final_submit_approved"] == 0
        authorized = automation.apply_application(
            int(app["id"]),
            final_submit_approved=True,
        )
        assert authorized["final_submit_approved"] == 1

        def unanswered_runner(_task: dict[str, object], _app: dict[str, object]) -> dict[str, object]:
            return {
                "status": "checkpoint",
                "checkpoint_kind": "unknown_field",
                "message": "A required question needs an answer.",
                "checkpoint": {
                    "fields": [
                        {
                            "question": "How did you hear about us?",
                            "type": "text",
                            "options": [],
                        }
                    ]
                },
            }

        checkpoint = automation.process_next_task(unanswered_runner)
        assert checkpoint and checkpoint["status"] == "checkpoint"
        assert checkpoint["checkpoint_kind"] == "unknown_field"
        assert len(checkpoint["events"]) >= 3

        continued = automation.resolve_checkpoint(
            int(checkpoint["id"]),
            {"How did you hear about us?": "Company career page"},
        )
        assert continued["status"] == "queued"
        saved = row(
            "SELECT answer FROM answer_rules WHERE question = ?",
            ("How did you hear about us?",),
        )
        assert saved and saved["answer"] == "Company career page"

        def review_runner(task: dict[str, object], _app: dict[str, object]) -> dict[str, object]:
            answers = automation._json_value(task["answers_json"], {})
            assert answers["How did you hear about us?"] == "Company career page"
            return {
                "status": "checkpoint",
                "checkpoint_kind": "final_review",
                "message": "Ready for final review.",
                "checkpoint": {},
            }

        review = automation.process_next_task(review_runner)
        assert review and review["checkpoint_kind"] == "final_review"
        try:
            automation.resolve_checkpoint(int(review["id"]))
        except ValueError as exc:
            assert "explicit" in str(exc)
        else:
            raise AssertionError("Final review continued without explicit submit approval")

        approved = automation.resolve_checkpoint(int(review["id"]), approve_submit=True)
        assert approved["final_submit_approved"] == 1

        def submitted_runner(task: dict[str, object], _app: dict[str, object]) -> dict[str, object]:
            assert task["final_submit_approved"] == 1
            return {
                "status": "submitted",
                "message": "Local fixture confirmed submission.",
                "result": {"confirmation_url": "https://example.test/thanks"},
            }

        completed = automation.process_next_task(submitted_runner)
        assert completed and completed["status"] == "submitted"
        stored_app = row("SELECT status FROM applications WHERE id = ?", (int(app["id"]),))
        assert stored_app and stored_app["status"] == "submitted"

    print("application workflow ok")


if __name__ == "__main__":
    main()
