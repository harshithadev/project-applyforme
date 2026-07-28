from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


class FakeSMTP:
    connected = 0
    logged_in = 0
    noop_calls = 0

    def __init__(self, host: str, port: int, timeout: int) -> None:
        assert host == "smtp.example.test"
        assert port == 587
        assert timeout == 15
        type(self).connected += 1

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def starttls(self) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        assert user == "candidate@example.test"
        assert password == "test-password"
        type(self).logged_in += 1

    def noop(self) -> None:
        type(self).noop_calls += 1


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp
        os.environ["SMTP_HOST"] = "smtp.example.test"
        os.environ["SMTP_PORT"] = "587"
        os.environ["SMTP_USER"] = "candidate@example.test"
        os.environ["SMTP_PASSWORD"] = "test-password"
        os.environ["EMAIL_FROM"] = "candidate@example.test"

        from job_agent import emailer, profile, readiness
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, row, rows, set_setting, setting

        init_db()
        capabilities = {
            "service": {
                "running": True,
                "installed": True,
                "pid": 123,
                "message": "Background service is running.",
            },
            "codex": {
                "ready": True,
                "available": True,
                "auth": "chatgpt",
                "message": "Codex CLI is signed in with ChatGPT.",
            },
            "latex_engine": "tectonic",
            "ocr": {
                "available": True,
                "message": "Local scanned-PDF OCR is ready.",
            },
            "browser": True,
            "email": {
                "configured": True,
                "available": True,
                "message": "Email can be sent.",
            },
            "document_watcher": {"running": True},
        }

        initial = readiness.evaluate_readiness(capabilities)
        assert initial["status"] == "blocked", initial
        assert "documents" in set(initial["summary"]["blocking"])
        assert "discovery" not in set(initial["summary"]["blocking"])
        discovery = next(
            item for item in initial["checks"] if item["id"] == "discovery"
        )
        assert discovery["status"] == "pass"
        assert discovery["detail"]["discovery_providers"] == 4
        graduate_targeting = next(
            item for item in initial["checks"] if item["id"] == "targeting"
        )
        assert graduate_targeting["status"] == "pass"
        assert graduate_targeting["view"] == "settings"
        assert graduate_targeting["detail"]["career_stage_mode"] == "graduate"
        assert graduate_targeting["detail"]["role_families"] == 6
        assert "management role family" in graduate_targeting["message"]
        assert not next(
            item for item in initial["modes"] if item["id"] == "review_automation"
        )["ready"]
        try:
            readiness.complete_setup(capabilities)
            raise AssertionError("Setup completed while required checks were blocked")
        except ValueError as exc:
            assert "documents" in str(exc)

        set_setting("target_role_families", "")
        missing_graduate_target = readiness.evaluate_readiness(capabilities)
        assert next(
            item
            for item in missing_graduate_target["checks"]
            if item["id"] == "targeting"
        )["status"] == "blocked"
        set_setting("additional_title_aliases", "transformation partner")
        custom_graduate_target = readiness.evaluate_readiness(capabilities)
        assert next(
            item
            for item in custom_graduate_target["checks"]
            if item["id"] == "targeting"
        )["status"] == "pass"
        set_setting(
            "target_role_families",
            (
                "product,project_program,agile_delivery,consulting,"
                "change_transformation,strategy_operations"
            ),
        )
        set_setting("additional_title_aliases", "")
        set_setting("career_stage_mode", "open")
        set_setting("role_keywords", "")
        missing_open_target = readiness.evaluate_readiness(capabilities)
        assert next(
            item
            for item in missing_open_target["checks"]
            if item["id"] == "targeting"
        )["status"] == "blocked"
        set_setting("role_keywords", "project coordinator")
        open_target = readiness.evaluate_readiness(capabilities)
        assert next(
            item for item in open_target["checks"] if item["id"] == "targeting"
        )["status"] == "pass"
        set_setting("career_stage_mode", "graduate")

        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nalex@example.test\nPython TypeScript platform engineer.",
            encoding="utf-8",
        )
        assert profile.ingest_docs()["ingested"] == 1
        set_setting("career_urls", "https://boards.example.test/careers")

        review_ready = readiness.evaluate_readiness(capabilities)
        assert review_ready["status"] == "ready", review_ready
        review_mode = next(
            item
            for item in review_ready["modes"]
            if item["id"] == "review_automation"
        )
        assert review_mode["ready"]
        assert not next(
            item for item in review_ready["modes"] if item["id"] == "outreach"
        )["ready"]

        verified = readiness.test_email_connection(
            lambda: emailer.verify_smtp_connection(FakeSMTP)
        )
        assert verified["status"] == "verified"
        assert FakeSMTP.connected == 1
        assert FakeSMTP.logged_in == 1
        assert FakeSMTP.noop_calls == 1
        assert setting("smtp_verification_status") == "verified"

        outreach_ready = readiness.evaluate_readiness(capabilities)
        assert next(
            item for item in outreach_ready["modes"] if item["id"] == "outreach"
        )["ready"]

        completed = readiness.complete_setup(capabilities)
        assert completed["setup_completed_at"]
        assert setting("setup_completed_at") == completed["setup_completed_at"]

        first_run = readiness.run_preflight(capabilities)
        assert first_run["status"] == "ready"
        assert first_run["score"] == 100
        assert len(rows("SELECT id FROM readiness_runs")) == 1
        assert readiness.readiness_state(capabilities)["history"][0]["id"] == first_run["run_id"]

        set_setting("mode", "rules_autonomous")
        set_setting("pipeline_enabled", "true")
        set_setting("pipeline_auto_approve", "true")
        set_setting("pipeline_auto_apply", "true")
        set_setting("browser_submit_enabled", "true")
        autonomous = readiness.evaluate_readiness(capabilities)
        assert next(
            item for item in autonomous["modes"] if item["id"] == "rules_autonomous"
        )["ready"]
        assert row("SELECT COUNT(*) AS count FROM events")["count"] >= 3

    print("readiness workflow ok")


if __name__ == "__main__":
    main()
