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


class CompanyFixture(BaseHTTPRequestHandler):
    requested: list[str] = []

    def do_GET(self) -> None:
        type(self).requested.append(self.path)
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /private\n"
            content_type = "text/plain"
        elif self.path == "/team":
            person = {
                "@context": "https://schema.org",
                "@type": "Person",
                "name": "Morgan Lee",
                "jobTitle": "Engineering Manager",
                "email": "morgan@example.test",
            }
            body = f"""<!doctype html><html><body>
              <script type="application/ld+json">{json.dumps(person)}</script>
              <article><h2>Riley Chen</h2><p>Technical Recruiter</p></article>
              <div>Recruiting Team <a href="mailto:careers@example.test">careers@example.test</a></div>
              <div>Customer Support <a href="mailto:support@example.test">support@example.test</a></div>
            </body></html>""".encode()
            content_type = "text/html"
        elif self.path == "/private":
            body = b"<html><body>Private Person, Hiring Manager, private@example.test</body></html>"
            content_type = "text/html"
        else:
            body = b"""<!doctype html><html><body>
              <a href="/team">Our Team</a>
              <a href="/private">Leadership</a>
              <a href="https://www.linkedin.com/company/example">LinkedIn</a>
            </body></html>"""
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def running_company() -> Iterator[str]:
    CompanyFixture.requested = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), CompanyFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def expect_value_error(action: object, text: str) -> None:
    try:
        action()
    except ValueError as exc:
        assert text.lower() in str(exc).lower(), exc
    else:
        raise AssertionError(f"Expected ValueError containing {text!r}")


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp, running_company() as company_url:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import applications, contact_discovery, jobs, outreach, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, row, rows, set_setting

        init_db()
        set_setting("contact_discovery_max_pages", "8")
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
                "url": company_url,
                "description": "Build Python and TypeScript services, reliable APIs, and SQL systems.",
            }
        )
        application = applications.draft_application(job_id)
        application_id = int(application["id"])

        run = contact_discovery.discover_for_application(application_id)
        assert run["status"] == "complete_with_warnings", run
        assert run["pages_scanned"] == 2
        assert run["contacts_added"] == 3, run
        assert run["candidates_found"] == 3, run
        assert "/robots.txt" in CompanyFixture.requested
        assert "/private" not in CompanyFixture.requested
        assert any("Robots policy" in message for message in json.loads(str(run["log"])))

        contacts = {item["email"]: item for item in outreach.list_contacts()}
        assert set(contacts) == {
            "careers@example.test",
            "morgan@example.test",
            "riley.chen@example.test",
        }
        manager = contacts["morgan@example.test"]
        assert manager["name"] == "Morgan Lee"
        assert manager["role"] == "Engineering Manager"
        assert manager["email_kind"] == "published"
        assert manager["verification_status"] == "published"
        assert manager["relevance_score"] >= 90
        assert manager["source_url"].endswith("/team")

        inferred = contacts["riley.chen@example.test"]
        assert inferred["email_kind"] == "inferred"
        assert inferred["verification_status"] == "unverified"
        expect_value_error(
            lambda: outreach.create_draft(application_id, int(inferred["id"])),
            "verify this contact",
        )
        verified = contact_discovery.verify_contact(int(inferred["id"]))
        assert verified["verification_status"] == "verified"
        assert verified["verified_at"]
        assert outreach.create_draft(application_id, int(verified["id"]))["status"] == "draft"
        assert outreach.create_draft(application_id, int(manager["id"]))["status"] == "draft"

        generic = contacts["careers@example.test"]
        generic_thread = outreach.create_draft(application_id, int(generic["id"]))
        rejected = contact_discovery.reject_contact(int(generic["id"]))
        assert rejected["verification_status"] == "rejected"
        expect_value_error(
            lambda: outreach.approve(int(generic_thread["id"])),
            "verify this contact",
        )
        expect_value_error(
            lambda: outreach.create_draft(application_id, int(generic["id"])),
            "verify this contact",
        )

        repeated = contact_discovery.discover_for_application(application_id)
        assert repeated["contacts_added"] == 0
        assert repeated["contacts_updated"] == 3
        assert len(rows("SELECT id FROM contacts")) == 3
        assert row("SELECT verification_status FROM contacts WHERE id = ?", (generic["id"],))[
            "verification_status"
        ] == "rejected"
        assert len(contact_discovery.list_runs()) == 2
        assert row("SELECT status FROM contact_discovery_runs WHERE id = ?", (run["id"],))["status"].startswith(
            "complete"
        )

        expect_value_error(
            lambda: contact_discovery.discover_for_application(
                application_id,
                "https://www.linkedin.com/company/example",
            ),
            "social-network",
        )

    print("contact discovery ok")


if __name__ == "__main__":
    main()
