from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeSMTP:
    messages: list[object] = []

    def __init__(self, _host: str, _port: int) -> None:
        pass

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, _user: str, _password: str) -> None:
        pass

    def send_message(self, message: object) -> None:
        type(self).messages.append(message)


def expect_value_error(action: object, message: str) -> None:
    try:
        action()
    except ValueError as exc:
        assert message.lower() in str(exc).lower(), exc
    else:
        raise AssertionError(f"Expected ValueError containing {message!r}")


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import applications, emailer, jobs, outreach, profile, writing
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, rows, set_setting

        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nPlatform engineer using Python, TypeScript, and SQL.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        assert profile.ingest_docs()["ingested"] == 1
        job_id = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/platform",
                "description": "Build Python APIs, TypeScript tools, and SQL systems.",
                "location": "Remote",
            }
        )
        application = applications.draft_application(job_id)
        application_id = int(application["id"])

        primary = outreach.create_contact(
            {
                "company": "ExampleCo",
                "name": "Morgan Lee",
                "role": "Engineering Manager",
                "email": "morgan@example.test",
            }
        )
        expect_value_error(
            lambda: outreach.create_contact(
                {"company": "ExampleCo", "email": "morgan@example.test"}
            ),
            "already exists",
        )
        mismatch = outreach.create_contact(
            {"company": "OtherCo", "email": "manager@other.example"}
        )
        expect_value_error(
            lambda: outreach.create_draft(application_id, int(mismatch["id"])),
            "does not match",
        )

        set_setting("email_mode", "approval")
        set_setting("daily_email_limit", "3")
        thread = outreach.create_draft(application_id, int(primary["id"]))
        thread_id = int(thread["id"])
        assert thread["status"] == "draft"
        assert thread["active_revision"]["version"] == 1
        assert thread["active_revision"]["body"].startswith("Hi Morgan,")
        assert outreach.create_draft(application_id, int(primary["id"]))["id"] == thread_id
        expect_value_error(lambda: outreach.queue(thread_id), "requires approving")

        revision = outreach.save_draft(
            thread_id,
            "Platform Engineer application",
            thread["active_revision"]["body"].replace("briefly introduce myself", "introduce myself"),
        )
        assert revision["active_revision"]["version"] == 2
        assert len(revision["revisions"]) == 2
        approved = outreach.approve(thread_id)
        assert approved["status"] == "approved"
        assert approved["approved_revision_id"] == approved["active_revision_id"]

        env_keys = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM"]
        old_env = {key: os.environ.get(key) for key in env_keys}
        old_smtp = emailer.smtplib.SMTP
        try:
            os.environ.update(
                {
                    "SMTP_HOST": "smtp.test",
                    "SMTP_PORT": "587",
                    "SMTP_USER": "user",
                    "SMTP_PASSWORD": "password",
                    "EMAIL_FROM": "sender@example.test",
                }
            )
            emailer.smtplib.SMTP = FakeSMTP
            assert emailer.send_email("direct@example.test", "Direct", "Body")["status"] == "blocked"
            assert outreach.queue(thread_id)["status"] == "queued"
            sent = outreach.process_next()
            assert sent and sent["status"] == "sent"
            assert sent["attempt_count"] == 1
            assert len(FakeSMTP.messages) == 1
            assert str(FakeSMTP.messages[0]["Message-ID"]).startswith("<applyforme-")
            assert outreach.queue(thread_id)["status"] == "sent"

            retry_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Riley", "email": "riley@example.test"}
            )
            retry_thread = outreach.create_draft(application_id, int(retry_contact["id"]))
            retry_thread = outreach.approve(int(retry_thread["id"]))
            outreach.queue(int(retry_thread["id"]))

            def fail_delivery(*_args: str) -> dict[str, str]:
                raise RuntimeError("temporary SMTP failure")

            failed = outreach.process_next(deliverer=fail_delivery)
            assert failed and failed["status"] == "failed"
            assert failed["attempt_count"] == 1
            assert "temporary SMTP failure" in failed["last_error"]
            outreach.queue(int(failed["id"]))
            retried = outreach.process_next()
            assert retried and retried["status"] == "sent"
            assert retried["attempt_count"] == 2

            limited_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Casey", "email": "casey@example.test"}
            )
            limited = outreach.create_draft(application_id, int(limited_contact["id"]))
            limited = outreach.approve(int(limited["id"]))
            set_setting("daily_email_limit", "2")
            expect_value_error(lambda: outreach.queue(int(limited["id"])), "daily email limit")

            autonomous_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Avery", "email": "avery@example.test"}
            )
            autonomous = outreach.create_draft(application_id, int(autonomous_contact["id"]))
            set_setting("daily_email_limit", "5")
            set_setting("email_mode", "autonomous")
            queued = outreach.queue(int(autonomous["id"]))
            assert queued["status"] == "queued"
            assert queued["approved_revision_id"] == queued["active_revision_id"]
            assert outreach.process_next()["status"] == "sent"

            set_setting("email_mode", "approval")
            exhausted_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Quinn", "email": "quinn@example.test"}
            )
            exhausted = outreach.create_draft(application_id, int(exhausted_contact["id"]))
            exhausted = outreach.approve(int(exhausted["id"]))
            for _attempt in range(3):
                outreach.queue(int(exhausted["id"]))
                exhausted = outreach.process_next(deliverer=fail_delivery)
                assert exhausted and exhausted["status"] == "failed"
            expect_value_error(lambda: outreach.queue(int(exhausted["id"])), "stopped after 3")

            stale_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Jordan", "email": "jordan@example.test"}
            )
            stale = outreach.create_draft(application_id, int(stale_contact["id"]))
            current = applications.get_application(application_id)["writing"]["current"]
            changed_content = copy.deepcopy(current["content"])
            changed_content["cover_letter"] = changed_content["cover_letter"].replace(
                "I would welcome", "I welcome"
            )
            changed = writing.save_manual_draft(application_id, changed_content)
            assert changed["status"] == "draft"
            expect_value_error(lambda: outreach.approve(int(stale["id"])), "writing changed")
            rebased = outreach.save_draft(
                int(stale["id"]),
                stale["active_revision"]["subject"],
                stale["active_revision"]["body"],
            )
            assert outreach.approve(int(rebased["id"]))["status"] == "approved"
        finally:
            emailer.smtplib.SMTP = old_smtp
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert len(rows("SELECT id FROM outreach_revisions WHERE thread_id = ?", (thread_id,))) == 2
        assert len(rows("SELECT id FROM outreach_threads")) == 6

    print("outreach workflow ok")


if __name__ == "__main__":
    main()
