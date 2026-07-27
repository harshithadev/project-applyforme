from __future__ import annotations

import json
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
        if self.path.startswith("/manual-confirmed"):
            body = (
                b"<!doctype html><html><body><h1>Thank you</h1>"
                b"<p>Your application was submitted.</p></body></html>"
            )
        elif self.path.startswith("/manual-submit"):
            body = b"""<!doctype html><html><body>
            <div class="captcha">Human-only submission control</div>
            <button id="manual-submit" onclick="window.location.href='/manual-confirmed'">
              Submit manually
            </button>
            </body></html>"""
        elif self.path.startswith("/manual-captcha"):
            if "manual_clear=ready" in self.headers.get("Cookie", ""):
                body = self.form(
                    """
                    <label for="name">Full name</label>
                    <input id="name" name="name" required>
                    """
                )
            else:
                body = b"""<!doctype html><html><body>
                <div class="captcha">Human verification required</div>
                <script>
                  console.error(
                    "diagnostic fixture alex@example.test 212-555-0199 password=fixture-password https://fixture.example.test/error?token=private"
                  );
                </script>
                <button id="manual-clear" onclick="
                  document.cookie='manual_clear=ready; Max-Age=3600; Path=/';
                  window.location.href='/manual-captcha?ats=workday&amp;cleared=1';
                ">Complete human verification</button>
                </body></html>"""
        elif self.path.startswith("/ashby/apply"):
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
            if "ats_session=ready" in self.headers.get("Cookie", ""):
                body = self.form(
                    """
                    <label for="name">Full name</label>
                    <input id="name" name="name" required>
                    """
                )
            else:
                body = b"""<!doctype html><html><body>
                <h1>Sign In</h1>
                <form action="/workday-auth" method="post">
                  <label for="username">Email</label>
                  <input id="username" name="username" type="email">
                  <label for="password">Password</label>
                  <input id="password" name="password" type="password">
                  <button type="submit">Sign in</button>
                </form>
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
        if self.path == "/workday-auth":
            self.send_response(303)
            self.send_header(
                "Set-Cookie",
                "ats_session=ready; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax",
            )
            self.send_header("Location", "/workday-login?ats=workday")
            self.end_headers()
            return
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

        from job_agent import (
            applications,
            automation,
            browser_diagnostics,
            browser_sessions,
            jobs,
            profile,
        )
        from job_agent.config import BROWSER_SESSIONS_DIR, DOCS_DIR
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
            assert browser_sessions._resume_url_allowed(
                f"{base_url}/manual-captcha",
                f"{base_url}/manual-captcha?cleared=1",
            )
            assert not browser_sessions._resume_url_allowed(
                f"{base_url}/manual-captcha",
                "https://different.example.test/application",
            )
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

            rejected_session = browser_sessions._run_login_handoff_for_test(
                int(login_result["id"]),
                lambda _page: None,
            )
            assert rejected_session["status"] == "needs_login", rejected_session
            still_paused = automation.get_task(int(login_result["id"]))
            assert still_paused and still_paused["checkpoint_kind"] == "login"

            def sign_in(page: object) -> None:
                page.locator("#username").fill("alex@example.test")
                page.locator("#password").fill("fixture-password")
                page.get_by_role("button", name="Sign in").click()
                page.wait_for_url("**/workday-login?ats=workday")

            saved_session = browser_sessions._run_login_handoff_for_test(
                int(login_result["id"]),
                sign_in,
            )
            assert saved_session["status"] == "ready", saved_session
            assert saved_session["last_verified_at"]
            resumed = automation.get_task(int(login_result["id"]))
            assert resumed and resumed["status"] == "queued", resumed
            login_submitted = automation.process_next_task()
            assert login_submitted and login_submitted["status"] == "submitted", login_submitted
            assert login_submitted["browser_session"]["status"] == "ready"

            captcha_app = draft_for_url(
                applications,
                jobs,
                "Manual CAPTCHA Engineer",
                f"{base_url}/manual-captcha?ats=workday",
            )
            automation.apply_application(int(captcha_app["id"]))
            captcha_task = automation.process_next_task()
            assert captcha_task and captcha_task["checkpoint_kind"] == "captcha"
            assert captcha_task["diagnostics"]
            captcha_diagnostic = captcha_task["diagnostics"][0]
            assert captcha_diagnostic["category"] == "captcha"
            assert captcha_diagnostic["retryable"] is True
            assert "manual takeover" in captcha_diagnostic["recommendation"].lower()
            diagnostic_record = browser_diagnostics.bundle_artifact(
                int(captcha_diagnostic["id"])
            )
            assert diagnostic_record
            diagnostic_path = Path(str(diagnostic_record["artifact_path"]))
            assert diagnostic_path.is_file()
            diagnostic_text = diagnostic_path.read_text(encoding="utf-8")
            diagnostic_payload = json.loads(diagnostic_text)
            assert diagnostic_payload["target_url"] == f"{base_url}/manual-captcha"
            assert diagnostic_payload["snapshot"]["url"] == f"{base_url}/manual-captcha"
            assert diagnostic_payload["snapshot"]["buttons"][0]["question"] == (
                "Complete human verification"
            )
            assert "[redacted-email]" in diagnostic_text
            assert "[redacted-phone]" in diagnostic_text
            assert "password=[redacted]" in diagnostic_text
            for private_value in (
                "alex@example.test",
                "212-555-0199",
                "fixture-password",
                "token=private",
                "?ats=workday",
                "manual_clear=ready",
            ):
                assert private_value not in diagnostic_text

            def clear_captcha(page: object) -> str:
                page.locator("#manual-clear").click()
                page.wait_for_url("**/manual-captcha?ats=workday&cleared=1")
                return "resume"

            captcha_session = browser_sessions._run_manual_takeover_for_test(
                int(captcha_task["id"]),
                clear_captcha,
            )
            assert captcha_session["status"] == "ready", captcha_session
            captcha_resumed = automation.get_task(int(captcha_task["id"]))
            assert captcha_resumed and captcha_resumed["status"] == "queued"
            assert "cleared=1" in captcha_resumed["resume_url"]
            captcha_submitted = automation.process_next_task()
            assert captcha_submitted and captcha_submitted["status"] == "submitted"

            manual_app = draft_for_url(
                applications,
                jobs,
                "Manual Submission Engineer",
                f"{base_url}/manual-submit?ats=workday",
            )
            automation.apply_application(int(manual_app["id"]))
            manual_task = automation.process_next_task()
            assert manual_task and manual_task["checkpoint_kind"] == "captcha"
            rejected_submission = browser_sessions._run_manual_takeover_for_test(
                int(manual_task["id"]),
                lambda _page: "submitted",
            )
            assert rejected_submission["status"] == "ready"
            manual_still_paused = automation.get_task(int(manual_task["id"]))
            assert manual_still_paused and manual_still_paused["status"] == "checkpoint"
            assert "not recorded" in manual_still_paused["message"]

            def submit_manually(page: object) -> str:
                page.locator("#manual-submit").click()
                page.wait_for_url("**/manual-confirmed")
                return "submitted"

            completed_submission = browser_sessions._run_manual_takeover_for_test(
                int(manual_task["id"]),
                submit_manually,
            )
            assert completed_submission["status"] == "ready"
            manual_submitted = automation.get_task(int(manual_task["id"]))
            assert manual_submitted and manual_submitted["status"] == "submitted"
            assert manual_submitted["result"]["manual_takeover"] is True
            assert manual_submitted["diagnostics"][0]["category"] == "submitted_manually"

            diagnostics_state = browser_diagnostics.dashboard_state()
            workday_health = next(
                item
                for item in diagnostics_state["adapter_health"]
                if item["adapter"] == "workday"
                and item["hostname"] == "127.0.0.1"
            )
            assert workday_health["attempts"] >= 5
            assert workday_health["submitted"] >= 4
            assert workday_health["manual_submissions"] == 1
            assert workday_health["category_counts"]["captcha"] >= 2

            profile_dirs = [path for path in BROWSER_SESSIONS_DIR.iterdir() if path.is_dir()]
            assert profile_dirs
            preferences = [
                path / "Default" / "Preferences"
                for path in profile_dirs
                if (path / "Default" / "Preferences").is_file()
            ]
            assert preferences
            assert any(
                '"credentials_enable_service":false'
                in path.read_text(encoding="utf-8").replace(" ", "")
                for path in preferences
            )
            cleared = browser_sessions.clear_session(int(saved_session["id"]))
            assert cleared["status"] == "cleared"
            assert not cleared["profile_present"]

        assert ATSFixture.submitted_paths == ["/thanks"] * 7

    print("browser submission ok")


if __name__ == "__main__":
    main()
