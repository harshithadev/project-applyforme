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

        from job_agent import applications, automation, browser_recovery, jobs, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import connect, init_db, now_iso, set_setting

        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nalex@example.test\n212-555-0199\n"
            "Platform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        profile.ingest_docs()
        set_setting("browser_retry_enabled", "true")
        set_setting("browser_retry_max_attempts", "3")
        set_setting("browser_retry_base_delay_seconds", "0")
        set_setting("browser_retry_max_delay_seconds", "0")
        set_setting("browser_circuit_failure_threshold", "2")
        set_setting("browser_circuit_cooldown_minutes", "30")

        def ready_application(sequence: int) -> dict[str, object]:
            job_id = jobs.add_manual_job(
                {
                    "title": "Platform Engineer",
                    "company": "Recovery Fixture",
                    "url": f"https://boards.greenhouse.io/recovery/jobs/{sequence}",
                    "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
                }
            )
            application = applications.draft_application(job_id)
            if application["resume_compile_status"] != "compiled":
                pdf_path = Path(str(application["resume_tex_path"])).with_suffix(".pdf")
                pdf_path.write_bytes(b"%PDF-1.4\n% recovery fixture\n")
                with connect() as conn:
                    conn.execute(
                        """
                        UPDATE applications
                        SET resume_compile_status = 'compiled', resume_pdf_path = ?,
                            resume_pdf_bytes = ?, resume_pdf_pages = 1
                        WHERE id = ?
                        """,
                        (
                            str(pdf_path),
                            pdf_path.stat().st_size,
                            int(application["id"]),
                        ),
                    )
            applications.approve_application(int(application["id"]))
            return applications.get_application(int(application["id"])) or application

        first_application = ready_application(1)
        first_task = automation.apply_application(int(first_application["id"]))

        def timeout_runner(
            _task: dict[str, object],
            _application: dict[str, object],
        ) -> dict[str, object]:
            raise TimeoutError(
                "Navigation timed out at "
                "https://boards.greenhouse.io/recovery/jobs/1?token=private"
            )

        first_failure = automation.process_next_task(timeout_runner)
        assert first_failure and first_failure["status"] == "retry_wait", first_failure
        assert first_failure["attempt_count"] == 1
        assert first_failure["retry_category"] == "timeout"
        assert first_failure["retry_reason"] == "recoverable_failure"
        assert any(
            event["step"] == "retry_wait" for event in first_failure["events"]
        )
        assert "token=private" not in str(first_failure["diagnostics"][0])

        second_failure = automation.process_next_task(timeout_runner)
        assert second_failure and second_failure["status"] == "failed", second_failure
        assert second_failure["attempt_count"] == 2
        assert second_failure["retry_exhausted"] == 1
        assert second_failure["retry_reason"] == "circuit_open"
        assert second_failure["recovery"]["circuit"]["effective_status"] == "open"

        second_application = ready_application(2)
        second_task = automation.apply_application(int(second_application["id"]))
        blocked_claim = automation.process_next_task(timeout_runner)
        assert blocked_claim is None
        held_task = automation.get_task(int(second_task["id"]))
        assert held_task and held_task["status"] == "retry_wait"
        assert held_task["attempt_count"] == 0
        assert held_task["retry_reason"] == "circuit_open"

        recovery_state = browser_recovery.dashboard_state()
        assert recovery_state["summary"]["open_circuits"] == 1
        assert recovery_state["summary"]["waiting"] == 1
        try:
            automation.retry_task(int(second_failure["id"]))
        except ValueError as exc:
            assert "circuit" in str(exc).lower()
        else:
            raise AssertionError("An open ATS circuit was bypassed without explicit reset")

        reset_retry = automation.retry_task(
            int(second_failure["id"]),
            reset_circuit=True,
        )
        assert reset_retry["status"] == "queued"
        released_task = automation.get_task(int(second_task["id"]))
        assert released_task and released_task["status"] == "queued"

        def submitted_runner(
            _task: dict[str, object],
            _application: dict[str, object],
        ) -> dict[str, object]:
            return {
                "status": "submitted",
                "message": "Recovery fixture submission confirmed.",
                "result": {
                    "confirmation_url": "https://boards.greenhouse.io/recovery/thanks"
                },
            }

        recovered = automation.process_next_task(submitted_runner)
        assert recovered and recovered["id"] == second_failure["id"]
        assert recovered["status"] == "submitted"
        assert recovered["attempt_count"] == 3
        assert recovered["recovery"]["circuit"]["effective_status"] == "closed"

        def uncertain_runner(
            task: dict[str, object],
            _application: dict[str, object],
        ) -> dict[str, object]:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE application_tasks
                    SET submit_started_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_iso(), now_iso(), int(task["id"])),
                )
            raise RuntimeError("Connection closed after the submit control was clicked")

        uncertain = automation.process_next_task(uncertain_runner)
        assert uncertain and uncertain["id"] == second_task["id"]
        assert uncertain["status"] == "checkpoint"
        assert uncertain["checkpoint_kind"] == "submission_uncertain"
        assert uncertain["attempt_count"] == 1
        assert uncertain["next_attempt_at"] == ""
        try:
            automation.retry_task(int(uncertain["id"]), reset_circuit=True)
        except ValueError as exc:
            assert "verify" in str(exc).lower()
        else:
            raise AssertionError("A submission-uncertain task was allowed to retry")

        third_application = ready_application(3)
        third_task = automation.apply_application(int(third_application["id"]))

        def unknown_runner(
            _task: dict[str, object],
            _application: dict[str, object],
        ) -> dict[str, object]:
            raise RuntimeError("Expected application control was not found")

        manual_review = automation.process_next_task(unknown_runner)
        assert manual_review and manual_review["id"] == third_task["id"]
        assert manual_review["status"] == "failed"
        assert manual_review["attempt_count"] == 1
        assert manual_review["retry_category"] == "automation_error"
        assert manual_review["retry_reason"] == "manual_review_required"
        assert manual_review["next_attempt_at"] == ""

        capped = browser_recovery.retry_decision(
            {
                "adapter": "greenhouse",
                "target_url": "https://boards.greenhouse.io/other/jobs/3",
                "attempt_count": 3,
                "submit_started_at": "",
            },
            message="Navigation timed out",
        )
        assert capped["should_retry"] is False
        assert capped["reason"] == "attempt_limit"
        assert capped["exhausted"] is True
        generic_failure = browser_recovery.retry_decision(
            {
                "adapter": "greenhouse",
                "target_url": "https://boards.greenhouse.io/other/jobs/4",
                "attempt_count": 1,
                "submit_started_at": "",
            },
            message="Expected application control was not found",
        )
        assert generic_failure["should_retry"] is False
        assert generic_failure["category"] == "automation_error"
        assert generic_failure["reason"] == "manual_review_required"

        with connect() as conn:
            conn.execute(
                """
                UPDATE adapter_circuit_breakers
                SET status = 'half_open'
                WHERE adapter = 'greenhouse'
                  AND hostname = 'boards.greenhouse.io'
                """
            )
        assert browser_recovery.recover_interrupted_circuits() == 1
        interrupted = browser_recovery.get_circuit(
            "greenhouse",
            "boards.greenhouse.io",
        )
        assert interrupted["effective_status"] == "open"
        assert interrupted["retry_after"]

    print("browser recovery ok")


if __name__ == "__main__":
    main()
