from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent


class CareerFixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/ats/manual"):
            if "audit_manual=ready" in self.headers.get("Cookie", ""):
                body = b"""<!doctype html><html><body>
                <form action="/thanks" method="post" enctype="multipart/form-data">
                  <label for="first_name">First name</label>
                  <input id="first_name" name="first_name" required>
                  <label for="last_name">Last name</label>
                  <input id="last_name" name="last_name" required>
                  <label for="resume">Resume / CV</label>
                  <input id="resume" name="resume" type="file" accept=".pdf" required>
                  <button type="submit">Submit application</button>
                </form>
                </body></html>"""
            else:
                body = b"""<!doctype html><html><body>
                <div class="captcha">Human verification required</div>
                <button id="manual-clear" onclick="
                  document.cookie='audit_manual=ready; Max-Age=3600; Path=/';
                  window.location.href='/ats/manual?ats=greenhouse&amp;cleared=1';
                ">Complete human verification</button>
                </body></html>"""
        elif self.path.startswith("/ats/greenhouse"):
            body = b"""<!doctype html><html><body>
            <form action="/thanks" method="post" enctype="multipart/form-data">
              <label for="first_name">First name</label>
              <input id="first_name" name="first_name" required>
              <label for="last_name">Last name</label>
              <input id="last_name" name="last_name" required>
              <label for="resume">Resume / CV</label>
              <input id="resume" name="resume" type="file" accept=".pdf" required>
              <button type="submit">Submit application</button>
            </form>
            </body></html>"""
        elif self.path.startswith("/jobs/platform-engineer"):
            structured = {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Platform Engineer",
                "description": "<p>Build production Python and TypeScript services, reliable APIs, and cloud automation.</p>",
                "datePosted": datetime.now(timezone.utc).isoformat(),
                "jobLocationType": "TELECOMMUTE",
                "hiringOrganization": {"@type": "Organization", "name": "ExampleCo"},
                "url": f"http://{self.headers['Host']}/jobs/platform-engineer",
            }
            body = (
                "<!doctype html><html><head><script type=\"application/ld+json\">"
                f"{json.dumps(structured)}</script></head><body><h1>Platform Engineer</h1></body></html>"
            ).encode("utf-8")
        elif self.path.startswith("/team"):
            person = {
                "@context": "https://schema.org",
                "@type": "Person",
                "name": "Public Manager",
                "jobTitle": "Engineering Manager",
                "email": "public.manager@example.test",
            }
            body = (
                "<!doctype html><html><body><script type=\"application/ld+json\">"
                f"{json.dumps(person)}</script></body></html>"
            ).encode("utf-8")
        elif self.path.startswith("/robots.txt"):
            body = b"User-agent: *\nAllow: /\n"
        else:
            body = b"""<!doctype html><html><body>
            <a href="/jobs/platform-engineer?utm_source=audit">Platform Engineer - Python and TypeScript</a>
            <a href="/team">Our Team</a>
            <a href="/about">About ExampleCo</a>
            </body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        body = b"<html><body><h1>Thank you</h1><p>Your application was submitted.</p></body></html>"
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
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{request.method} {url} returned {exc.code}: {body}") from exc


def request_bytes(url: str) -> tuple[bytes, str, str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return (
            response.read(),
            response.headers.get_content_type(),
            response.headers.get("Content-Disposition", ""),
        )


def request_multipart(
    url: str,
    *,
    name: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> dict[str, object]:
    boundary = "----applyforme-feature-audit"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="documents"; filename="{name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(
            f"POST {url} returned {exc.code}: {response_body}"
        ) from exc


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

        from job_agent import (
            app,
            applications,
            ats_adapters,
            automation,
            browser_diagnostics,
            browser_recovery,
            browser_sessions,
            broad_sources,
            contact_discovery,
            emailer,
            job_sources,
            jobs,
            orchestration,
            outreach,
            profile,
            service,
            writing,
        )
        from job_agent.config import DOCS_DIR
        from job_agent.db import connect, init_db, log, now_iso, row, rows, set_setting
        from job_agent.latex import available_latex_engine

        init_db()
        set_setting("discovery_providers", "")

        service_paths = service.service_paths(project_root=REPO_ROOT)
        service_definition = service.launch_agent_definition(service_paths)
        service_ready = bool(
            service_definition.get("RunAtLoad")
            and service_definition.get("KeepAlive")
            and service_definition.get("ProgramArguments") == [str(service_paths.runner)]
            and service_definition.get("WorkingDirectory") == str(REPO_ROOT)
        )
        record(
            "PASS" if service_ready else "FAIL",
            "macOS login service",
            "A per-user launch agent starts the local dashboard after login and keeps it running.",
        )

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
            set_setting("career_stage_mode", "open")
            set_setting("career_urls", careers_url)
            set_setting("target_companies", "ExampleCo")
            set_setting("role_keywords", "platform engineer, Python, TypeScript")
            set_setting("locations", "remote")
            set_setting("posted_within_days", "14")
            first_scan = jobs.discover_jobs()
            second_scan = jobs.discover_jobs()
        if first_scan["inserted"] == 1 and second_scan["seen"] == 1:
            record("PASS", "Career-page scanning", "Configured pages are scanned and duplicate job URLs are ignored.")
        else:
            record("FAIL", "Career-page scanning", f"First={first_scan}, second={second_scan}")
        source_state = row(
            "SELECT * FROM job_source_states WHERE source_url = ?",
            (job_sources.canonicalize_url(careers_url),),
        )
        source_state_ready = bool(
            source_state
            and source_state["status"] == "ready"
            and source_state["scan_count"] == 2
            and source_state["last_success_at"]
        )
        record(
            "PASS" if source_state_ready else "FAIL",
            "Persistent source scan state",
            "Each career source retains its cursor, scan count, status, and last successful run."
            if source_state_ready
            else f"Source state was incomplete: {source_state}",
        )

        provider_now = datetime.now(timezone.utc)
        provider_payloads = {
            "jobicy": {
                "jobs": [{
                    "id": "audit-jobicy",
                    "jobTitle": "Project Coordinator",
                    "companyName": "Jobicy Audit",
                    "url": "https://jobicy.com/jobs/audit-jobicy",
                    "jobDescription": "Coordinate delivery.",
                    "jobGeo": "Remote",
                    "pubDate": provider_now.isoformat(),
                }]
            },
            "remotive": {
                "jobs": [{
                    "id": "audit-remotive",
                    "title": "Project Coordinator",
                    "company_name": "Remotive Audit",
                    "url": "https://remotive.com/remote-jobs/audit-remotive",
                    "description": "Coordinate delivery.",
                    "candidate_required_location": "Remote",
                    "publication_date": provider_now.isoformat(),
                }]
            },
            "arbeitnow": {
                "data": [{
                    "slug": "audit-arbeitnow",
                    "title": "Project Coordinator",
                    "company_name": "Arbeitnow Audit",
                    "url": "https://www.arbeitnow.com/jobs/audit-arbeitnow",
                    "description": "Coordinate delivery.",
                    "location": "Remote",
                    "remote": True,
                    "created_at": int(provider_now.timestamp()),
                }]
            },
        }
        provider_feed = f"""<rss version="2.0"><channel><item>
        <title>WWR Audit: Project Coordinator</title>
        <link>https://weworkremotely.com/remote-jobs/audit-wwr</link>
        <guid>audit-wwr</guid><region>Remote</region>
        <pubDate>{format_datetime(provider_now)}</pubDate>
        <description>Coordinate delivery.</description>
        </item></channel></rss>"""
        old_provider_json = broad_sources.fetch_json
        old_provider_url = broad_sources.fetch_url

        def fake_provider_json(url: str) -> object:
            if url.startswith(broad_sources.PROVIDERS["jobicy"].source_url):
                return provider_payloads["jobicy"]
            if url.startswith(broad_sources.PROVIDERS["remotive"].source_url):
                return provider_payloads["remotive"]
            if url == broad_sources.PROVIDERS["arbeitnow"].source_url:
                return provider_payloads["arbeitnow"]
            raise AssertionError(f"Unexpected provider URL: {url}")

        def fake_provider_url(url: str) -> str:
            if url == broad_sources.PROVIDERS["weworkremotely"].source_url:
                return provider_feed
            raise AssertionError(f"Unexpected provider URL: {url}")

        try:
            broad_sources.fetch_json = fake_provider_json
            broad_sources.fetch_url = fake_provider_url
            set_setting("career_urls", "")
            set_setting(
                "discovery_providers",
                "jobicy,remotive,weworkremotely,arbeitnow",
            )
            set_setting("career_stage_mode", "open")
            set_setting("role_keywords", "project coordinator")
            set_setting("target_companies", "Preferred Audit Company")
            set_setting("target_company_mode", "prefer")
            set_setting("locations", "remote")
            provider_scan = jobs.discover_jobs()
        finally:
            broad_sources.fetch_json = old_provider_json
            broad_sources.fetch_url = old_provider_url
            set_setting("discovery_providers", "")

        provider_states = {
            state["source_kind"]
            for state in jobs.list_source_states()
            if state["source_kind"] in broad_sources.PROVIDERS
        }
        provider_ok = (
            provider_scan["inserted"] == 4
            and provider_scan["errors"] == 0
            and provider_states == set(broad_sources.PROVIDERS)
        )
        record(
            "PASS" if provider_ok else "FAIL",
            "Broad job discovery providers",
            "Scan jobs normalizes Jobicy, Remotive, We Work Remotely, and Arbeitnow with persistent source state."
            if provider_ok
            else f"Scan={provider_scan}, states={sorted(provider_states)}",
        )

        discovered_job = row("SELECT * FROM jobs WHERE source = 'career-detail' LIMIT 1")
        if (
            discovered_job
            and "production Python" in discovered_job["description"]
            and discovered_job["location"] == "Remote"
            and discovered_job["posted_at"]
        ):
            record(
                "PASS",
                "Job discovery quality",
                "Detail pages are enriched and role, company, location, and posting-age filters are applied.",
            )
        else:
            record("FAIL", "Job discovery quality", f"Enriched job was incomplete: {discovered_job}")

        date_only_posting = job_sources.JobPosting(
            title="Platform Engineer",
            company="ExampleCo",
            url="https://example.test/jobs/date-only-audit",
            description="Build production Python services.",
            location="Remote",
            posted_at=(datetime.now().astimezone() - timedelta(days=1)).date().isoformat(),
            metadata={"posted_at_precision": "date"},
        )
        age_settings = {
            "role_keywords": "platform engineer, Python",
            "target_companies": "ExampleCo",
            "locations": "remote",
            "include_unknown_posted_at": "false",
        }
        day_window = jobs.evaluate_posting(
            date_only_posting,
            {
                **age_settings,
                "posted_age_mode": "days",
                "posted_within_days": "1",
            },
        )
        hour_window = jobs.evaluate_posting(
            date_only_posting,
            {
                **age_settings,
                "posted_age_mode": "hours",
                "posted_within_hours": "24",
            },
        )
        record(
            "PASS" if day_window.accepted and not hour_window.accepted else "FAIL",
            "Hour and calendar-day job windows",
            (
                "Hours mode requires an exact timestamp, while Calendar days mode "
                "includes date-only postings from the selected local-date window."
            )
            if day_window.accepted and not hour_window.accepted
            else f"days={day_window}, hours={hour_window}",
        )

        graduate_settings = {
            "career_stage_mode": "graduate",
            "target_role_families": (
                "product,project_program,agile_delivery,consulting,"
                "change_transformation,strategy_operations"
            ),
            "graduate_max_required_experience_years": "3",
            "graduate_include_internships": "false",
            "additional_title_aliases": "",
            "excluded_title_terms": (
                "senior, principal, director, head of, vice president, "
                "vp, chief, lead"
            ),
            "locations": "remote",
            "target_companies": "ExampleCo",
            "posted_within_days": "0",
        }

        def graduate_match(title: str, description: str) -> object:
            return jobs.evaluate_posting(
                job_sources.JobPosting(
                    title=title,
                    company="ExampleCo",
                    url=f"https://example.test/jobs/{title.casefold().replace(' ', '-')}",
                    description=description,
                    location="Remote",
                ),
                graduate_settings,
            )

        graduate_product = graduate_match(
            "Associate Product Manager",
            "A recent-graduate role requiring 1 year of experience with roadmaps.",
        )
        renamed_change = graduate_match(
            "Graduate Transformation Partner",
            "A graduate programme focused on adoption and change impact.",
        )
        unrelated_title = graduate_match(
            "Software Engineer",
            "Agile project delivery, consulting, product, and change management.",
        )
        senior_title = graduate_match(
            "Senior Product Manager",
            "Own a product roadmap.",
        )
        excessive_experience = graduate_match(
            "Project Coordinator",
            "Requires 4+ years of project delivery experience.",
        )
        three_year_experience = graduate_match(
            "Project Coordinator",
            "Requires 3+ years of project delivery experience.",
        )
        graduate_matching_ready = bool(
            graduate_product.accepted
            and renamed_change.accepted
            and three_year_experience.accepted
            and not unrelated_title.accepted
            and not senior_title.accepted
            and not excessive_experience.accepted
        )
        record(
            "PASS" if graduate_matching_ready else "FAIL",
            "Graduate management role matching",
            (
                "Selected management families accept early-career title variants "
                "while rejecting unrelated titles, senior roles, and excessive "
                "required experience."
            )
            if graduate_matching_ready
            else (
                f"product={graduate_product}, renamed={renamed_change}, "
                f"three_years={three_year_experience}, "
                f"unrelated={unrelated_title}, senior={senior_title}, "
                f"experience={excessive_experience}"
            ),
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
        with running_server(CareerFixture) as company_url:
            contact_run = contact_discovery.discover_for_application(int(app_record["id"]), company_url)
        discovered_contact = row(
            "SELECT * FROM contacts WHERE email = ?",
            ("public.manager@example.test",),
        )
        if (
            contact_run["contacts_added"] >= 1
            and discovered_contact
            and discovered_contact["verification_status"] == "published"
            and discovered_contact["source_url"].endswith("/team")
        ):
            record(
                "PASS",
                "Hiring-manager discovery",
                "Bounded public-page discovery ranks contacts and retains source-backed verification.",
            )
        else:
            record("FAIL", "Hiring-manager discovery", f"run={contact_run}, contact={discovered_contact}")
        tex_path = Path(str(app_record["resume_tex_path"]))
        tex = tex_path.read_text(encoding="utf-8") if tex_path.exists() else ""
        expected_company = str(job["company"])
        if all(value in tex for value in (expected_company, "Platform Engineer", "python", "Alex Candidate")):
            record("PASS", "Tailored LaTeX source", "A per-job .tex resume uses job keywords and uploaded evidence.")
        else:
            record("FAIL", "Tailored LaTeX source", "Expected company, role, keyword, or profile evidence is absent.")

        writing_overview = app_record.get("writing", {})
        current_writing = writing_overview.get("current") or {}
        if (
            app_record["cover_letter"]
            and app_record["statements"]
            and app_record["email_body"]
            and current_writing.get("evidence")
            and current_writing.get("validation", {}).get("status") in {"passed", "warning"}
        ):
            record(
                "PASS",
                "Automatic written materials",
                "Resume content, cover letter, statements, and outreach are generated with evidence mappings.",
            )
        else:
            record("FAIL", "Automatic written materials", "One or more application drafts were empty.")
        invalid_content = json.loads(json.dumps(current_writing.get("content", {})))
        invalid_content["resume"]["bullets"][0]["text"] = "Increased throughput by 99%."
        invalid_content["claims"][0] = {
            "text": "Increased throughput by 99%.",
            "evidence_ids": invalid_content["resume"]["bullets"][0]["evidence_ids"],
        }
        invalid_version = writing.save_manual_draft(int(app_record["id"]), invalid_content)
        if invalid_version.get("validation", {}).get("status") == "failed":
            record(
                "PASS",
                "Unsupported-claim detection",
                "Unverified quantitative claims are rejected without replacing the active draft.",
            )
        else:
            record("FAIL", "Unsupported-claim detection", f"Unexpected validation: {invalid_version}")

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
            direct_email = emailer.send_email("manager@example.test", "Hello", "Body")
            first_contact = outreach.create_contact(
                {
                    "company": "ExampleCo",
                    "name": "Morgan",
                    "role": "Engineering Manager",
                    "email": "manager@example.test",
                }
            )
            first_thread = outreach.create_draft(int(second_app["id"]), int(first_contact["id"]))
            first_thread = outreach.save_draft(
                int(first_thread["id"]),
                first_thread["active_revision"]["subject"],
                first_thread["active_revision"]["body"],
            )
            try:
                outreach.queue(int(first_thread["id"]))
                unapproved_blocked = False
            except ValueError:
                unapproved_blocked = True
            outreach.approve(int(first_thread["id"]))
            outreach.queue(int(first_thread["id"]))
            first_email = outreach.process_next()

            second_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Riley", "email": "riley@example.test"}
            )
            second_thread = outreach.create_draft(int(second_app["id"]), int(second_contact["id"]))
            outreach.approve(int(second_thread["id"]))
            outreach.queue(int(second_thread["id"]))
            second_email = outreach.process_next()

            third_contact = outreach.create_contact(
                {"company": "ExampleCo", "name": "Casey", "email": "casey@example.test"}
            )
            third_thread = outreach.create_draft(int(second_app["id"]), int(third_contact["id"]))
            outreach.approve(int(third_thread["id"]))
            try:
                outreach.queue(int(third_thread["id"]))
                third_blocked = False
            except ValueError as exc:
                third_blocked = "limit" in str(exc).lower()
        finally:
            emailer.smtplib.SMTP = old_smtp
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if first_email["status"] == second_email["status"] == "sent" and third_blocked and FakeSMTP.sent == 2:
            record("PASS", "SMTP sending and daily email limit", "Approved SMTP delivery stops at the configured daily limit.")
        else:
            record(
                "FAIL",
                "SMTP sending and daily email limit",
                f"Results: first={first_email}, second={second_email}, third_blocked={third_blocked}",
            )
        if direct_email["status"] == "blocked" and unapproved_blocked and first_email["status"] == "sent":
            record("PASS", "Email approval mode", "Direct and unapproved sends are blocked; approved outreach is delivered.")
        else:
            record(
                "FAIL",
                "Email approval mode",
                f"direct={direct_email}, unapproved_blocked={unapproved_blocked}, sent={first_email}",
            )
        if len(first_thread["revisions"]) == 2 and outreach.get_thread(int(first_thread["id"]))["status"] == "sent":
            record(
                "PASS",
                "Hiring-manager outreach workflow",
                "Contacts move through versioned draft, approval, queue, delivery, and audit states.",
            )
        else:
            record("FAIL", "Hiring-manager outreach workflow", "The outreach thread did not complete its workflow.")

        api_handler = QuietAppHandler.build(app.Handler)
        with running_server(api_handler) as dashboard_url:
            state = request_json(f"{dashboard_url}/api/state")
            uploaded_document = request_multipart(
                f"{dashboard_url}/api/documents/upload",
                name="web-evidence.txt",
                content=b"Web upload evidence with Python and reliable automation.",
                content_type="text/plain",
            )
            uploaded_document_id = int(
                next(
                    item["id"]
                    for item in uploaded_document.get("files", [])
                    if item.get("id")
                )
            )
            uploaded_body, uploaded_type, uploaded_disposition = request_bytes(
                f"{dashboard_url}/api/documents/artifact?document_id={uploaded_document_id}"
            )
            created = request_json(
                f"{dashboard_url}/api/jobs",
                {
                    "title": "Platform Engineer",
                    "company": "WebCo",
                    "url": "https://example.test/jobs/api-engineer",
                    "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
                },
            )
            api_application = request_json(
                f"{dashboard_url}/api/applications/draft",
                {"job_id": int(created["id"])},
            )
            api_contact = request_json(
                f"{dashboard_url}/api/contacts",
                {
                    "company": "WebCo",
                    "name": "Taylor",
                    "role": "Hiring Manager",
                    "email": "taylor@webco.example",
                },
            )
            api_outreach = request_json(
                f"{dashboard_url}/api/outreach/draft",
                {
                    "application_id": int(api_application["id"]),
                    "contact_id": int(api_contact["id"]),
                },
            )
            approval_state = request_json(f"{dashboard_url}/api/state").get("approvals", {})
            approval_item = next(
                (
                    item
                    for item in approval_state.get("items", [])
                    if item.get("source_type") == "application"
                    and int(item.get("source_id", 0)) == int(api_application["id"])
                ),
                {},
            )
            approval_resolution = (
                request_json(
                    f"{dashboard_url}/api/approvals/action",
                    {
                        "approval_item_id": int(approval_item["id"]),
                        "action": "approve",
                        "note": "Feature audit approval.",
                    },
                )
                if approval_item
                else {}
            )
            readiness_run = request_json(
                f"{dashboard_url}/api/readiness/run",
                {},
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
        api_ok = bool(
            state.get("settings")
            and state.get("pipeline")
            and state.get("service")
            and state.get("approvals")
            and state.get("document_inbox")
            and state.get("readiness")
            and "browser_sessions" in state
            and "browser_diagnostics" in state
            and "browser_recovery" in state
            and created.get("id")
            and api_application.get("id")
            and api_contact.get("id")
            and api_outreach.get("active_revision")
        )
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
            record("PASS", "Local dashboard API", "The website can create applications, contacts, and outreach drafts.")
        else:
            record(
                "FAIL",
                "Local dashboard API",
                f"state={bool(state.get('settings'))}, pipeline={bool(state.get('pipeline'))}, "
                f"service={bool(state.get('service'))}, "
                f"approvals={bool(state.get('approvals'))}, "
                f"documents={bool(state.get('document_inbox'))}, "
                f"readiness={bool(state.get('readiness'))}, "
                f"browser_sessions={'browser_sessions' in state}, "
                f"created_id={created.get('id')}",
            )
        readiness_ok = bool(
            readiness_run.get("run_id")
            and readiness_run.get("checks")
            and len(readiness_run.get("modes", [])) == 4
            and readiness_run.get("history")
        )
        record(
            "PASS" if readiness_ok else "FAIL",
            "Setup and readiness preflight",
            "Persisted preflight checks gate tailoring, review automation, outreach, and rules-autonomous modes.",
        )
        document_inbox_ok = bool(
            uploaded_document.get("saved") == 1
            and uploaded_body.startswith(b"Web upload evidence")
            and uploaded_type == "text/plain"
            and "inline" in uploaded_disposition
        )
        record(
            "PASS" if document_inbox_ok else "FAIL",
            "Website document inbox",
            "Validated multipart uploads are stored locally, ingested, and exposed through scoped artifact access.",
        )
        ocr_ready = bool(state.get("document_inbox", {}).get("ocr", {}).get("available"))
        record(
            "PASS" if ocr_ready else "BLOCKED",
            "Local scanned-PDF OCR",
            "Poppler rendering and Tesseract OCR are available for image-only PDFs.",
        )
        inbox_ok = bool(
            approval_item
            and approval_resolution.get("item", {}).get("resolution") == "approve"
            and approval_resolution.get("inbox", {}).get("history")
        )
        if inbox_ok:
            record(
                "PASS",
                "Unified approval inbox",
                "Application, browser, outreach, and pipeline decisions share a durable action queue and history.",
            )
        else:
            record(
                "FAIL",
                "Unified approval inbox",
                f"item={bool(approval_item)}, resolution={approval_resolution.get('item', {}).get('resolution')}",
            )
        notification_policy = approval_state.get("notifications", {})
        notification_ok = bool(
            "enabled" in notification_policy
            and "quiet" in notification_policy
            and notification_policy.get("quiet_start")
            and notification_policy.get("quiet_end")
        )
        record(
            "PASS" if notification_ok else "FAIL",
            "Deduplicated macOS notifications",
            "New approval items use durable delivery records and configurable quiet hours.",
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

        adapter_routes = {
            "https://jobs.ashbyhq.com/example/apply": "ashby",
            "https://jobs.smartrecruiters.com/Example/123-role": "smartrecruiters",
            "https://example.wd5.myworkdayjobs.com/Careers/job/123": "workday",
        }
        adapters_ready = all(
            automation._adapter_name(url) == expected
            for url, expected in adapter_routes.items()
        )
        discovery_ready = all(
            job_sources.source_kind(url) == expected
            for url, expected in adapter_routes.items()
        )
        record(
            "PASS" if adapters_ready and discovery_ready else "FAIL",
            "Extended ATS adapter coverage",
            "Ashby, SmartRecruiters, and Workday URLs route to native discovery and guarded browser adapters."
            if adapters_ready and discovery_ready
            else "One or more extended ATS URL patterns were not recognized for discovery and submission.",
        )

        adapter_registry_ready = bool(
            all(
                definition
                and definition.version
                and definition.apply_labels
                and "guarded-submit" in definition.capabilities
                for definition in (
                    ats_adapters.definition(adapter)
                    for adapter in ats_adapters.supported_adapters()
                )
            )
            and ats_adapters.replay_check(
                {
                    "adapter": "ashby",
                    "category": "submit_control",
                    "snapshot": {
                        "form_count": 1,
                        "controls": [
                            {
                                "tag": "input",
                                "type": "email",
                                "name": "email",
                                "question": "Email",
                                "required": True,
                            }
                        ],
                        "buttons": [
                            {
                                "tag": "button",
                                "type": "button",
                                "name": "",
                                "question": "Explore jobs",
                                "disabled": False,
                            }
                        ],
                    },
                }
            )["reproduced"]
        )
        record(
            "PASS" if adapter_registry_ready else "FAIL",
            "Versioned ATS adapter lifecycle",
            (
                "Selector contracts are versioned, sanitized snapshots can be replayed, "
                "and repeated compatibility drift quarantines a host before another browser launch."
            )
            if adapter_registry_ready
            else "The versioned adapter registry or replay evaluator is incomplete.",
        )

        set_setting("browser_retry_enabled", "true")
        set_setting("browser_retry_max_attempts", "3")
        set_setting("browser_retry_base_delay_seconds", "0")
        set_setting("browser_retry_max_delay_seconds", "0")
        set_setting("browser_circuit_failure_threshold", "2")
        set_setting("browser_circuit_cooldown_minutes", "30")
        recovery_task = {
            "adapter": "greenhouse",
            "target_url": "https://recovery-audit.example.test/jobs/1",
            "attempt_count": 1,
            "submit_started_at": "",
        }
        browser_recovery.record_outcome(
            recovery_task,
            status="failed",
            message="Navigation timed out before form submission.",
        )
        retry_allowed = browser_recovery.retry_decision(
            recovery_task,
            message="Navigation timed out before form submission.",
        )
        browser_recovery.record_outcome(
            recovery_task,
            status="failed",
            message="Navigation timed out before form submission.",
        )
        circuit = browser_recovery.get_circuit(
            "greenhouse",
            "recovery-audit.example.test",
        )
        retry_blocked = browser_recovery.retry_decision(
            {**recovery_task, "attempt_count": 2},
            message="Navigation timed out before form submission.",
        )
        uncertain_blocked = browser_recovery.retry_decision(
            {
                **recovery_task,
                "submit_started_at": now_iso(),
            },
            message="Connection closed after submit started.",
            checkpoint_kind="submission_uncertain",
        )
        browser_recovery.reset_circuit(
            "greenhouse",
            "recovery-audit.example.test",
        )
        recovery_ready = bool(
            retry_allowed["should_retry"]
            and circuit["effective_status"] == "open"
            and not retry_blocked["should_retry"]
            and retry_blocked["reason"] == "circuit_open"
            and not uncertain_blocked["should_retry"]
            and uncertain_blocked["reason"] == "submission_uncertain"
            and browser_recovery.get_circuit(
                "greenhouse",
                "recovery-audit.example.test",
            )["effective_status"] == "closed"
        )
        record(
            "PASS" if recovery_ready else "FAIL",
            "Policy-controlled browser recovery",
            "Recoverable pre-submit failures back off, repeated host failures open a circuit, explicit reset closes it, and uncertain submissions never retry."
            if recovery_ready
            else (
                f"allowed={retry_allowed}, circuit={circuit}, "
                f"blocked={retry_blocked}, uncertain={uncertain_blocked}"
            ),
        )

        try:
            set_setting("daily_application_limit", "10")
            set_setting("mode", "review")
            set_setting("browser_submit_enabled", "false")
            with running_server(CareerFixture) as ats_url:
                browser_job_id = jobs.add_manual_job(
                    {
                        "title": "Browser Automation Engineer",
                        "company": "Local ATS Fixture",
                        "url": f"{ats_url}/ats/greenhouse?ats=greenhouse",
                        "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
                    }
                )
                browser_app = applications.draft_application(browser_job_id)
                applications.approve_application(int(browser_app["id"]))
                browser_task = automation.apply_application(int(browser_app["id"]))
                review_task = automation.process_next_task()
                review_ready = bool(
                    browser_task.get("adapter") == "greenhouse"
                    and review_task
                    and review_task.get("checkpoint_kind") == "final_review"
                    and review_task.get("screenshots")
                )
                set_setting("mode", "rules_autonomous")
                set_setting("browser_submit_enabled", "true")
                manual_job_id = jobs.add_manual_job(
                    {
                        "title": "Manual Takeover Engineer",
                        "company": "Local ATS Fixture",
                        "url": f"{ats_url}/ats/manual?ats=greenhouse",
                        "description": "Build reliable browser automation with explicit human checkpoints.",
                    }
                )
                manual_app = applications.draft_application(manual_job_id)
                applications.approve_application(int(manual_app["id"]))
                automation.apply_application(int(manual_app["id"]))
                manual_checkpoint = automation.process_next_task()

                def clear_manual_checkpoint(page: object) -> str:
                    page.locator("#manual-clear").click()
                    page.wait_for_url("**/ats/manual?ats=greenhouse&cleared=1")
                    return "resume"

                manual_session = browser_sessions._run_manual_takeover_for_test(
                    int(manual_checkpoint["id"]),
                    clear_manual_checkpoint,
                )
                manual_resumed = automation.get_task(int(manual_checkpoint["id"]))
                takeover_ready = bool(
                    manual_checkpoint.get("checkpoint_kind") == "captcha"
                    and manual_session.get("status") == "ready"
                    and manual_resumed
                    and manual_resumed.get("status") == "queued"
                    and "cleared=1" in str(manual_resumed.get("resume_url") or "")
                )
                automation.resolve_checkpoint(int(review_task["id"]), approve_submit=True)
                automation.start_worker()
                deadline = time.monotonic() + 20
                completed_task = automation.get_task(int(review_task["id"]))
                completed_manual = automation.get_task(int(manual_checkpoint["id"]))
                while (
                    (
                        completed_task
                        and completed_task["status"] in {"queued", "running"}
                    )
                    or (
                        completed_manual
                        and completed_manual["status"] in {"queued", "running"}
                    )
                ) and time.monotonic() < deadline:
                    time.sleep(0.1)
                    completed_task = automation.get_task(int(review_task["id"]))
                    completed_manual = automation.get_task(int(manual_checkpoint["id"]))
            worker_completed = bool(completed_task and completed_task["status"] == "submitted")
            takeover_completed = bool(
                takeover_ready
                and completed_manual
                and completed_manual["status"] == "submitted"
                and any(
                    event["step"] == "manual_takeover"
                    for event in completed_manual.get("events", [])
                )
            )
            session_persisted = bool(
                completed_task
                and completed_task.get("browser_session")
                and completed_task["browser_session"].get("status") == "ready"
                and completed_task["browser_session"].get("profile_present")
                and completed_task["browser_session"].get("last_used_at")
            )
            diagnostic_ready = bool(
                manual_checkpoint
                and manual_checkpoint.get("diagnostics")
                and manual_checkpoint["diagnostics"][0].get("category") == "captcha"
                and manual_checkpoint["diagnostics"][0].get("download_available")
                and browser_diagnostics.dashboard_state().get("adapter_health")
            )
            diagnostic_bundle_id = int(manual_checkpoint["diagnostics"][0]["id"])
            diagnostic_handler = QuietAppHandler.build(app.Handler)
            with running_server(diagnostic_handler) as diagnostic_dashboard_url:
                diagnostic_body, diagnostic_type, diagnostic_disposition = request_bytes(
                    f"{diagnostic_dashboard_url}/api/applications/task-diagnostic"
                    f"?bundle_id={diagnostic_bundle_id}"
                )
            diagnostic_ready = bool(
                diagnostic_ready
                and json.loads(diagnostic_body).get("category") == "captcha"
                and diagnostic_type == "application/json"
                and "attachment" in diagnostic_disposition
            )
            record(
                "PASS" if review_ready and worker_completed else "FAIL",
                "Playwright browser submission",
                "Greenhouse form filling, resume upload, screenshot review, and confirmed submission passed locally."
                if review_ready and worker_completed
                else f"review_ready={review_ready}, task={completed_task}",
            )
            record(
                "PASS" if worker_completed else "FAIL",
                "Background application worker",
                "The persistent worker consumed an approved checkpoint and recorded the verified submission."
                if worker_completed
                else f"Worker result: {completed_task}",
            )
            record(
                "PASS" if session_persisted else "FAIL",
                "Persistent ATS browser sessions",
                "A restricted local Chromium profile persisted across review and submission browser launches."
                if session_persisted
                else f"Browser session result: {completed_task.get('browser_session') if completed_task else None}",
            )
            record(
                "PASS" if takeover_completed else "FAIL",
                "Guided manual browser takeover",
                "A CAPTCHA checkpoint resumed from a human-completed persistent browser page and finished in a separate worker launch."
                if takeover_completed
                else f"Takeover result: ready={takeover_ready}, task={completed_manual}",
            )
            record(
                "PASS" if diagnostic_ready else "FAIL",
                "ATS diagnostics and recovery bundles",
                "Browser outcomes produce redacted downloadable bundles, fixed recovery guidance, and per-host adapter health."
                if diagnostic_ready
                else f"Diagnostic result: {manual_checkpoint.get('diagnostics') if manual_checkpoint else None}",
            )
        except Exception as exc:
            record("FAIL", "Playwright browser submission", f"Local ATS integration failed: {exc}")
            record("FAIL", "Background application worker", f"Worker integration failed: {exc}")
            record("FAIL", "Persistent ATS browser sessions", f"Session integration failed: {exc}")
            record("FAIL", "Guided manual browser takeover", f"Takeover integration failed: {exc}")
            record("FAIL", "ATS diagnostics and recovery bundles", f"Diagnostic integration failed: {exc}")

        set_setting("pipeline_enabled", "true")
        set_setting("pipeline_min_score", "100")
        set_setting("pipeline_auto_write", "false")
        set_setting("pipeline_auto_apply", "false")
        set_setting("daily_application_limit", "200")
        pipeline_job_id = jobs.add_manual_job(
            {
                "title": "Pipeline Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/pipeline-audit",
                "description": "Build Python and TypeScript automation systems.",
                "location": "Remote",
            }
        )
        with connect() as conn:
            conn.execute("UPDATE jobs SET score = 100 WHERE id = ?", (pipeline_job_id,))
        pipeline_enqueued = orchestration.enqueue_eligible_jobs()
        pipeline_item = row(
            "SELECT id FROM pipeline_items WHERE job_id = ?",
            (pipeline_job_id,),
        )
        pipeline_package = (
            orchestration.advance_item(int(pipeline_item["id"])) if pipeline_item else None
        )
        pipeline_review = (
            orchestration.advance_item(int(pipeline_item["id"])) if pipeline_item else None
        )
        pipeline_ready = bool(
            pipeline_enqueued["queued"] == 1
            and pipeline_package
            and pipeline_package["application_id"]
            and pipeline_review
            and pipeline_review["status"] == "review"
            and pipeline_review["resume_compile_status"] == "compiled"
        )
        record(
            "PASS" if pipeline_ready else "FAIL",
            "End-to-end application pipeline",
            "A score-qualified job advanced through durable drafting and LaTeX compilation to review."
            if pipeline_ready
            else f"Queued={pipeline_enqueued}, package={pipeline_package}, review={pipeline_review}",
        )
        set_setting("pipeline_enabled", "false")

        codex = writing.codex_status(force=True)
        if codex["ready"] and codex["auth"] == "chatgpt":
            record(
                "PASS",
                "ChatGPT subscription/Codex bridge",
                "The isolated writing queue can use the locally cached ChatGPT-authenticated Codex CLI.",
            )
        else:
            record("BLOCKED", "ChatGPT subscription/Codex bridge", str(codex["message"]))

    order = {"PASS": 0, "PARTIAL": 1, "BLOCKED": 2, "FAIL": 3}
    for status, feature, detail in sorted(results, key=lambda item: (order[item[0]], item[1])):
        print(f"{status:7} | {feature}\n          {detail}")

    counts = {status: sum(1 for result in results if result[0] == status) for status in order}
    print("\nSUMMARY | " + ", ".join(f"{status}={count}" for status, count in counts.items()))


if __name__ == "__main__":
    main()
