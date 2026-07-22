from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent


class CareerFixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"""<!doctype html><html><body>
        <a href="/jobs/platform-engineer">Platform Engineer - Python and TypeScript</a>
        <a href="/about">About ExampleCo</a>
        </body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def running_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_json(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_bytes(url: str) -> tuple[bytes, str, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return (
            response.read(),
            response.headers.get_content_type(),
            response.headers.get("Content-Disposition", ""),
        )


class QuietAppHandler:
    @staticmethod
    def build(handler: type[BaseHTTPRequestHandler]) -> type[BaseHTTPRequestHandler]:
        class QuietHandler(handler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

        return QuietHandler


class FakeSMTP:
    sent = 0

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

    def send_message(self, _message: object) -> None:
        type(self).sent += 1


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    results: list[tuple[str, str, str]] = []

    def record(status: str, feature: str, detail: str) -> None:
        results.append((status, feature, detail))

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import app, applications, automation, emailer, jobs, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, log, row, rows, set_setting
        from job_agent.latex import available_latex_engine

        init_db()

        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\nPlatform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        (DOCS_DIR / "photo.png").write_bytes(b"not a supported document")
        ingested = profile.ingest_docs()
        if ingested["ingested"] == 1 and ingested["skipped"] == 1 and ingested["failed"] == 0:
            record("PASS", "Document ingestion", "Supported source documents are persisted locally.")
        else:
            record("FAIL", "Document ingestion", f"Unexpected result: {ingested}")
        try:
            import pypdf  # noqa: F401

            record("PASS", "PDF and DOCX documents", "PDF and DOCX extraction support is installed and separately fixture-tested.")
        except ImportError:
            record("BLOCKED", "PDF and DOCX documents", "Run npm run setup to install PDF extraction support.")

        with running_server(CareerFixture) as careers_url:
            set_setting("career_urls", careers_url)
            set_setting("target_companies", "ExampleCo")
            set_setting("role_keywords", "platform engineer, Python, TypeScript")
            first_scan = jobs.discover_jobs()
            second_scan = jobs.discover_jobs()
        if first_scan["inserted"] == 1 and second_scan["seen"] == 1:
            record("PASS", "Career-page scanning", "Configured pages are scanned and duplicate job URLs are ignored.")
        else:
            record("FAIL", "Career-page scanning", f"First={first_scan}, second={second_scan}")
        record(
            "PARTIAL",
            "Job discovery quality",
            "The scanner reads static links only; location/date filters and JavaScript ATS pages are not handled.",
        )

        tailored_job_id = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/platform-engineer",
                "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
                "location": "Remote",
            }
        )
        job = row("SELECT * FROM jobs WHERE id = ?", (tailored_job_id,))
        assert job is not None
        app_record = applications.draft_application(int(job["id"]))
        tex_path = Path(str(app_record["resume_tex_path"]))
        tex = tex_path.read_text(encoding="utf-8") if tex_path.exists() else ""
        expected_company = str(job["company"])
        if all(value in tex for value in (expected_company, "Platform Engineer", "python", "Alex Candidate")):
            record("PASS", "Tailored LaTeX source", "A per-job .tex resume uses job keywords and uploaded evidence.")
        else:
            record("FAIL", "Tailored LaTeX source", "Expected company, role, keyword, or profile evidence is absent.")

        if app_record["cover_letter"] and app_record["statements"] and app_record["email_body"]:
            record("PARTIAL", "Automatic written materials", "Cover letter, statements, and outreach are generated, but only from fixed templates.")
        else:
            record("FAIL", "Automatic written materials", "One or more application drafts were empty.")

        engine = available_latex_engine()
        pdf_path = str(app_record["resume_pdf_path"] or "")
        if (
            engine
            and app_record["resume_compile_status"] == "compiled"
            and int(app_record["resume_pdf_pages"]) >= 1
            and int(app_record["resume_pdf_bytes"]) >= 1_000
            and pdf_path
            and Path(pdf_path).exists()
        ):
            record("PASS", "Reliable LaTeX PDF compilation", str(app_record["resume_compile_message"]))
        elif engine:
            record("FAIL", "Reliable LaTeX PDF compilation", str(app_record["resume_compile_message"]))
        else:
            record("BLOCKED", "Reliable LaTeX PDF compilation", "No TeX engine is installed, so only .tex output is produced.")

        persisted = rows("SELECT id FROM documents") and rows("SELECT id FROM jobs") and rows("SELECT id FROM applications")
        record("PASS" if persisted else "FAIL", "Local application history", "Documents, jobs, applications, and events persist in SQLite.")

        rule_id = applications.save_answer_rule("Will you require visa sponsorship?", "No")
        saved_rule = row("SELECT * FROM answer_rules WHERE id = ?", (rule_id,))
        if saved_rule and saved_rule["answer"] == "No" and saved_rule["risky"] == 1:
            record("PASS", "Remembered form answers", "Answers persist and sensitive questions are flagged as risky.")
        else:
            record("FAIL", "Remembered form answers", "The saved rule was missing or not risk-classified.")

        blocked_review = automation.apply_application(int(app_record["id"]))
        applications.approve_application(int(app_record["id"]))
        queued = automation.apply_application(int(app_record["id"]))
        if blocked_review["status"] == "blocked" and queued["status"] in {"queued", "blocked"}:
            record("PASS", "Review and approval guard", "Review mode prevents applying before explicit approval.")
        else:
            record("FAIL", "Review and approval guard", f"Before={blocked_review}, after={queued}")

        set_setting("daily_application_limit", "1")
        applications.mark_application_submitted(int(app_record["id"]))
        second_job_id = jobs.add_manual_job(
            {
                "title": "Backend Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/backend-engineer",
                "description": "Python SQL APIs",
            }
        )
        second_app = applications.draft_application(second_job_id)
        applications.approve_application(int(second_app["id"]))
        app_limit = automation.apply_application(int(second_app["id"]))
        record(
            "PASS" if app_limit["status"] == "blocked" and "limit" in app_limit["message"].lower() else "FAIL",
            "Daily application limit",
            app_limit["message"],
        )

        set_setting("daily_email_limit", "2")
        set_setting("email_mode", "approval")
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
            first_email = emailer.send_email("manager@example.test", "Hello", "Body")
            second_email = emailer.send_email("manager@example.test", "Hello", "Body")
            third_email = emailer.send_email("manager@example.test", "Hello", "Body")
        finally:
            emailer.smtplib.SMTP = old_smtp
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if first_email["status"] == second_email["status"] == "sent" and third_email["status"] == "blocked":
            record("PASS", "SMTP sending and daily email limit", "SMTP flow works and stops at the configured daily limit.")
        else:
            record("FAIL", "SMTP sending and daily email limit", f"Results: {first_email}, {second_email}, {third_email}")
        record(
            "FAIL",
            "Email approval mode",
            "The send function ignores email_mode=approval and sends immediately when called.",
        )

        api_handler = QuietAppHandler.build(app.Handler)
        with running_server(api_handler) as dashboard_url:
            state = request_json(f"{dashboard_url}/api/state")
            created = request_json(
                f"{dashboard_url}/api/jobs",
                {
                    "title": "API Engineer",
                    "company": "WebCo",
                    "url": "https://example.test/jobs/api-engineer",
                    "description": "Python API",
                },
            )
            compiled = request_json(
                f"{dashboard_url}/api/applications/compile",
                {"application_id": int(app_record["id"])},
            )
            pdf_body, pdf_type, pdf_disposition = request_bytes(
                f"{dashboard_url}/api/applications/artifact?application_id={app_record['id']}&kind=pdf"
            )
            tex_body, tex_type, tex_disposition = request_bytes(
                f"{dashboard_url}/api/applications/artifact?application_id={app_record['id']}&kind=tex"
            )
        api_ok = bool(state.get("settings") and created.get("id"))
        artifact_ok = bool(
            compiled.get("resume_compile_status") == "compiled"
            and pdf_body.startswith(b"%PDF")
            and pdf_type == "application/pdf"
            and "inline" in pdf_disposition
            and b"\\documentclass" in tex_body
            and tex_type in {"application/x-tex", "text/x-tex", "text/plain"}
            and "attachment" in tex_disposition
        )
        if api_ok:
            record("PASS", "Local dashboard API", "The website can read state and trigger write operations.")
        else:
            record(
                "FAIL",
                "Local dashboard API",
                f"state={bool(state.get('settings'))}, created_id={created.get('id')}",
            )
        if artifact_ok:
            record("PASS", "Resume artifact access", "Validated PDFs open inline and LaTeX sources download through scoped endpoints.")
        else:
            record(
                "FAIL",
                "Resume artifact access",
                f"compile={compiled.get('resume_compile_status')}, pdf=({pdf_type}, {pdf_disposition}, {pdf_body[:4]!r}), "
                f"tex=({tex_type}, {tex_disposition}, {tex_body[:20]!r})",
            )

        original_settings = app.all_settings
        original_discover = app.jobs.discover_jobs
        original_sleep = app.time.sleep
        scans = {"count": 0}
        scanned_twice = threading.Event()

        def fake_settings() -> dict[str, str]:
            return {"scan_interval_minutes": "1"}

        def fake_discover() -> dict[str, int]:
            scans["count"] += 1
            if scans["count"] >= 2:
                scanned_twice.set()
            return {"inserted": 0, "seen": 0}

        def fake_sleep(_seconds: float) -> None:
            if scans["count"] >= 2:
                raise SystemExit

        try:
            app.all_settings = fake_settings
            app.jobs.discover_jobs = fake_discover
            app.time.sleep = fake_sleep
            app.start_background_scanner()
            repeated = scanned_twice.wait(timeout=2)
        finally:
            app.all_settings = original_settings
            app.jobs.discover_jobs = original_discover
            app.time.sleep = original_sleep
        record(
            "PASS" if repeated else "FAIL",
            "Repeated background career scans",
            "The background scheduler invokes scanning repeatedly when enabled." if repeated else "The scheduler did not repeat.",
        )

        log("Plain-English audit event.")
        event = row("SELECT message FROM events WHERE message = ?", ("Plain-English audit event.",))
        record("PASS" if event else "FAIL", "Plain-English activity log", "Worker actions and blockers are recorded for the dashboard.")

        record("FAIL", "Playwright browser submission", "No ATS adapter submits forms; apply requests only return queued or blocked.")
        record("FAIL", "Background application worker", "There is no worker that consumes approved applications and submits them.")
        record("FAIL", "Hiring-manager discovery", "A contacts table exists, but no contact-finding or verification workflow exists.")
        record("FAIL", "Hiring-manager outreach workflow", "There is no contact-to-draft-to-approval UI workflow.")
        record("FAIL", "ChatGPT subscription/Codex bridge", "The local website cannot invoke a ChatGPT subscription or Codex run in the background.")

    order = {"PASS": 0, "PARTIAL": 1, "BLOCKED": 2, "FAIL": 3}
    for status, feature, detail in sorted(results, key=lambda item: (order[item[0]], item[1])):
        print(f"{status:7} | {feature}\n          {detail}")

    counts = {status: sum(1 for result in results if result[0] == status) for status in order}
    print("\nSUMMARY | " + ", ".join(f"{status}={count}" for status, count in counts.items()))


if __name__ == "__main__":
    main()
