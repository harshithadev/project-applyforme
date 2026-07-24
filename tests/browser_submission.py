from __future__ import annotations

import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent


class ATSFixture(BaseHTTPRequestHandler):
    submitted_paths: list[str] = []

    def do_GET(self) -> None:
        if self.path.startswith("/ashby/apply"):
            body = self.form(
                """
                <label for="name">Name</label>
                <input id="name" name="_systemfield_name" required>
                """,
                include_phone=True,
            )
        elif self.path.startswith("/smartrecruiters/step1"):
            body = self.stepped_form(
                """
                <label for="first_name">First name</label>
                <input id="first_name" name="firstName" required>
                <label for="last_name">Last name</label>
                <input id="last_name" name="lastName" required>
                <label for="email">Email</label>
                <input id="email" name="email" type="email" required>
                <label for="resume">Resume</label>
                <input id="resume" name="resume" type="file" accept=".pdf" required>
                """,
                "/smartrecruiters/step2?ats=smartrecruiters",
            )
        elif self.path.startswith("/smartrecruiters/step2"):
            body = self.final_step(
                "Phone",
                "phone",
                "tel",
                """
                <label for="privacy">I agree to the privacy notice</label>
                <input id="privacy" name="privacy" type="checkbox" required>
                """,
            )
        elif self.path.startswith("/smartrecruiters"):
            body = b"""<!doctype html><html><body>
            <h1>SmartRecruiters Platform Engineer</h1>
            <a href="/smartrecruiters/step1?ats=smartrecruiters">I'm interested</a>
            </body></html>"""
        elif self.path.startswith("/workday-login"):
            body = b"""<!doctype html><html><body>
            <h1>Sign In</h1>
            <label for="username">Email</label>
            <input id="username" type="email">
            <label for="password">Password</label>
            <input id="password" type="password">
            </body></html>"""
        elif self.path.startswith("/workday/choice"):
            body = b"""<!doctype html><html><body>
            <a data-automation-id="applyManually" href="/workday/step1?ats=workday">Apply Manually</a>
            </body></html>"""
        elif self.path.startswith("/workday/step1"):
            body = self.stepped_form(
                """
                <label for="name">Full name</label>
                <input id="name" name="name" required>
                <label for="email">Email</label>
                <input id="email" name="email" type="email" required>
                <label for="resume">Resume / CV</label>
                <input id="resume" name="resume" type="file" accept=".pdf" required>
                """,
                "/workday/step2?ats=workday",
                "Save and Continue",
            )
        elif self.path.startswith("/workday/step2"):
            body = self.final_step("Phone", "phone", "tel")
        elif self.path.startswith("/workday"):
            body = b"""<!doctype html><html><body>
            <h1>Workday Platform Engineer</h1>
            <a data-automation-id="jobPostingApplyButton" href="/workday/choice?ats=workday">Apply</a>
            </body></html>"""
        elif self.path.startswith("/greenhouse"):
            body = self.form(
                """
                <label for="first_name">First name</label>
                <input id="first_name" name="first_name" required>
                <label for="last_name">Last name</label>
                <input id="last_name" name="last_name" required>
                """
            )
        elif self.path.startswith("/lever/apply"):
            body = self.form(
                """
                <label for="name">Full name</label>
                <input id="name" name="name" required>
                """
            )
        elif self.path.startswith("/lever"):
            body = b"""<!doctype html><html><body>
            <h1>Lever Platform Engineer</h1>
            <a class="postings-btn" href="/lever/apply?ats=lever">Apply for this job</a>
            </body></html>"""
        else:
            self.send_error(404)
            return
        self.respond(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        type(self).submitted_paths.append(self.path)
        self.respond(
            b"<!doctype html><html><body><h1>Thank you</h1>"
            b"<p>Your application was submitted.</p></body></html>"
        )

    def form(self, identity_fields: str, include_phone: bool = True) -> bytes:
        return f"""<!doctype html>
        <html><body>
          <form action="/thanks" method="post" enctype="multipart/form-data">
            {identity_fields}
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required>
            {"<label for='phone'>Phone</label><input id='phone' name='phone' type='tel' required>" if include_phone else ""}
            <label for="resume">Resume / CV</label>
            <input id="resume" name="resume" type="file" accept=".pdf" required>
            {""
              if "first_name" not in identity_fields
              else '''<label for="sponsorship">Will you require visa sponsorship?</label>
              <select id="sponsorship" name="sponsorship" required>
                <option value="">Choose</option>
                <option value="Yes">Yes</option>
                <option value="No">No</option>
              </select>'''}
            <button type="submit">Submit application</button>
          </form>
        </body></html>""".encode("utf-8")

    def stepped_form(self, fields: str, next_url: str, label: str = "Next") -> bytes:
        return f"""<!doctype html><html><body>
        <form>
          {fields}
          <button type="button" onclick="window.location.href='{next_url}'">{label}</button>
        </form>
        </body></html>""".encode("utf-8")

    def final_step(
        self,
        label: str,
        name: str,
        field_type: str,
        extra_fields: str = "",
    ) -> bytes:
        return f"""<!doctype html><html><body>
        <form action="/thanks" method="post">
          <label for="{name}">{label}</label>
          <input id="{name}" name="{name}" type="{field_type}" required>
          {extra_fields}
          <button type="submit">Submit application</button>
        </form>
        </body></html>""".encode("utf-8")

    def respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def fixture_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ATSFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def draft_for_url(
    applications: object,
    jobs: object,
    title: str,
    url: str,
) -> dict[str, object]:
    job_id = jobs.add_manual_job(
        {
            "title": title,
            "company": "Local ATS Fixture",
            "url": url,
            "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
        }
    )
    app = applications.draft_application(job_id)
    assert app["resume_compile_status"] == "compiled", app["resume_compile_message"]
    applications.approve_application(int(app["id"]))
    return app


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import applications, automation, jobs, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, row, set_setting

        init_db()
        assert automation.playwright_available(), "Python Playwright package is not installed"
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nalex@example.test\n212-555-0199\n"
            "Platform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        profile.ingest_docs()
        set_setting("daily_application_limit", "10")
        set_setting("browser_headless", "true")
        ATSFixture.submitted_paths = []
        expected_adapters = {
            "https://jobs.ashbyhq.com/example/apply": "ashby",
            "https://jobs.smartrecruiters.com/Example/123-role": "smartrecruiters",
            "https://example.wd5.myworkdayjobs.com/Careers/job/123": "workday",
        }
        for url, adapter in expected_adapters.items():
            assert automation._adapter_name(url) == adapter

        with fixture_server() as base_url:
            greenhouse_app = draft_for_url(
                applications,
                jobs,
                "Greenhouse Platform Engineer",
                f"{base_url}/greenhouse?ats=greenhouse",
            )
            greenhouse_task = automation.apply_application(int(greenhouse_app["id"]))
            assert greenhouse_task["adapter"] == "greenhouse"
            applications.save_answer_rule("Will you require visa sponsorship?", "No")
            sensitive = automation.process_next_task()
            assert sensitive and sensitive["checkpoint_kind"] == "sensitive_question", sensitive
            automation.resolve_checkpoint(
                int(sensitive["id"]),
                {"Will you require visa sponsorship?": "No"},
            )
            review = automation.process_next_task()
            assert review and review["checkpoint_kind"] == "final_review", review
            assert review["screenshots"]
            screenshot = Path(str(review["artifact_dir"])) / str(review["screenshots"][-1])
            assert screenshot.is_file() and screenshot.stat().st_size > 1_000
            automation.resolve_checkpoint(int(review["id"]), approve_submit=True)
            submitted = automation.process_next_task()
            assert submitted and submitted["status"] == "submitted", submitted

            set_setting("mode", "rules_autonomous")
            set_setting("browser_submit_enabled", "true")
            lever_app = draft_for_url(
                applications,
                jobs,
                "Lever Platform Engineer",
                f"{base_url}/lever?ats=lever",
            )
            lever_task = automation.apply_application(int(lever_app["id"]))
            assert lever_task["adapter"] == "lever"
            lever_result = automation.process_next_task()
            assert lever_result and lever_result["status"] == "submitted", lever_result
            stored = row(
                "SELECT status FROM applications WHERE id = ?",
                (int(lever_app["id"]),),
            )
            assert stored and stored["status"] == "submitted"

            ashby_app = draft_for_url(
                applications,
                jobs,
                "Ashby Platform Engineer",
                f"{base_url}/ashby/apply?ats=ashby",
            )
            ashby_task = automation.apply_application(int(ashby_app["id"]))
            assert ashby_task["adapter"] == "ashby"
            ashby_result = automation.process_next_task()
            assert ashby_result and ashby_result["status"] == "submitted", ashby_result

            smartrecruiters_app = draft_for_url(
                applications,
                jobs,
                "SmartRecruiters Platform Engineer",
                f"{base_url}/smartrecruiters?ats=smartrecruiters",
            )
            smartrecruiters_task = automation.apply_application(int(smartrecruiters_app["id"]))
            assert smartrecruiters_task["adapter"] == "smartrecruiters"
            applications.save_answer_rule("I agree to the privacy notice", "agree")
            smartrecruiters_consent = automation.process_next_task()
            assert (
                smartrecruiters_consent
                and smartrecruiters_consent["checkpoint_kind"] == "sensitive_question"
            ), smartrecruiters_consent
            automation.resolve_checkpoint(
                int(smartrecruiters_consent["id"]),
                {"I agree to the privacy notice": "agree"},
            )
            smartrecruiters_result = automation.process_next_task()
            assert smartrecruiters_result and smartrecruiters_result["status"] == "submitted", smartrecruiters_result
            assert any(event["step"] == "advancing" for event in smartrecruiters_result["events"])

            workday_app = draft_for_url(
                applications,
                jobs,
                "Workday Platform Engineer",
                f"{base_url}/workday?ats=workday",
            )
            workday_task = automation.apply_application(int(workday_app["id"]))
            assert workday_task["adapter"] == "workday"
            workday_result = automation.process_next_task()
            assert workday_result and workday_result["status"] == "submitted", workday_result
            assert any("manual application" in event["message"].lower() for event in workday_result["events"])

            login_app = draft_for_url(
                applications,
                jobs,
                "Workday Login Engineer",
                f"{base_url}/workday-login?ats=workday",
            )
            login_task = automation.apply_application(int(login_app["id"]))
            assert login_task["adapter"] == "workday"
            login_result = automation.process_next_task()
            assert login_result and login_result["checkpoint_kind"] == "login", login_result
            try:
                automation.resolve_checkpoint(int(login_result["id"]))
            except ValueError as exc:
                assert "manually" in str(exc)
            else:
                raise AssertionError("Login checkpoint was incorrectly re-queued")
            automation.cancel_task(int(login_result["id"]))

        assert ATSFixture.submitted_paths == ["/thanks"] * 5

    print("browser submission ok")


if __name__ == "__main__":
    main()
