from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent


class DiscoveryFixture(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            body = """<!doctype html><html><body>
            <a href="/jobs/platform?utm_source=first">Platform Engineer</a>
            <a href="/jobs/platform?utm_campaign=duplicate">Platform Engineer duplicate</a>
            <a href="/jobs/sales">Sales Manager</a>
            <a href="/jobs/old">Platform Engineer II</a>
            <a href="/jobs/boston">Platform Engineer - Boston</a>
            <a href="/jobs/other-company">Platform Engineer - OtherCo</a>
            <a href="/about">About</a>
            </body></html>"""
        else:
            today = datetime.now(timezone.utc)
            posting = {
                "/jobs/platform": {
                    "title": "Platform Engineer",
                    "company": "ExampleCo",
                    "date": today,
                    "location_type": "TELECOMMUTE",
                    "description": "<p>Build production Python services and reliable cloud automation.</p>",
                },
                "/jobs/sales": {
                    "title": "Sales Manager",
                    "company": "ExampleCo",
                    "date": today,
                    "location_type": "TELECOMMUTE",
                    "description": "<p>Lead account sales and customer relationships.</p>",
                },
                "/jobs/old": {
                    "title": "Platform Engineer II",
                    "company": "ExampleCo",
                    "date": today - timedelta(days=60),
                    "location_type": "TELECOMMUTE",
                    "description": "<p>Build Python platform services.</p>",
                },
                "/jobs/boston": {
                    "title": "Platform Engineer",
                    "company": "ExampleCo",
                    "date": today,
                    "location": {"address": {"addressLocality": "Boston", "addressRegion": "MA"}},
                    "description": "<p>Build Python platform services.</p>",
                },
                "/jobs/other-company": {
                    "title": "Platform Engineer",
                    "company": "OtherCo",
                    "date": today,
                    "location_type": "TELECOMMUTE",
                    "description": "<p>Build Python platform services.</p>",
                },
            }[path]
            structured = {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": posting["title"],
                "description": posting["description"],
                "datePosted": posting["date"].isoformat(),
                "hiringOrganization": {"@type": "Organization", "name": posting["company"]},
                "url": f"http://{self.headers['Host']}{path}",
            }
            if posting.get("location_type"):
                structured["jobLocationType"] = posting["location_type"]
            if posting.get("location"):
                structured["jobLocation"] = posting["location"]
            body = (
                "<!doctype html><html><head><script type=\"application/ld+json\">"
                f"{json.dumps(structured)}</script></head><body><h1>{posting['title']}</h1></body></html>"
            )
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def running_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DiscoveryFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import job_sources, jobs
        from job_agent.db import init_db, rows, set_setting

        init_db()
        set_setting("target_companies", "ExampleCo")
        set_setting("role_keywords", "platform engineer, Python")
        set_setting("locations", "remote")
        set_setting("posted_within_days", "14")

        with running_server() as career_url:
            set_setting("career_urls", career_url)
            first = jobs.discover_jobs()
            second = jobs.discover_jobs()

        assert first == {"inserted": 1, "seen": 0, "filtered": 4, "errors": 0, "sources": 1}, first
        assert second == {"inserted": 0, "seen": 1, "filtered": 4, "errors": 0, "sources": 1}, second
        generic_jobs = rows("SELECT * FROM jobs WHERE source = 'career-detail'")
        assert len(generic_jobs) == 1
        generic = generic_jobs[0]
        assert generic["title"] == "Platform Engineer"
        assert generic["company"] == "ExampleCo"
        assert generic["location"] == "Remote"
        assert "production Python services" in generic["description"]
        assert "utm_" not in generic["url"]
        assert generic["posted_at"] and int(generic["score"]) >= 80
        assert "Role:" in generic["match_reasons"]
        language_match = jobs.evaluate_posting(
            job_sources.JobPosting(
                title="C++ Engineer",
                company="ExampleCo",
                url="https://example.test/jobs/cpp",
                description="Build modern C++ services.",
                location="Remote",
            ),
            {
                "role_keywords": "C++",
                "target_companies": "ExampleCo",
                "locations": "remote",
                "posted_within_days": "0",
            },
        )
        assert language_match.accepted and language_match.score >= 90

        now = datetime.now(timezone.utc).isoformat()
        greenhouse_payload = {
            "jobs": [
                {
                    "id": 101,
                    "title": "Software Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/101?gh_src=campaign",
                    "location": {"name": "New York, NY"},
                    "content": "&lt;p&gt;Build Python APIs &amp;amp; automation.&lt;/p&gt;",
                    "updated_at": now,
                }
            ]
        }
        greenhouse = job_sources.parse_greenhouse_payload(greenhouse_payload, "Acme")
        assert len(greenhouse) == 1
        assert greenhouse[0].description == "Build Python APIs & automation."
        assert greenhouse[0].external_id == "101"
        assert "gh_src" not in greenhouse[0].url

        lever_payload = [
            {
                "id": "lever-202",
                "text": "Backend Engineer",
                "categories": {"location": "United States", "team": "Engineering"},
                "descriptionPlain": "Build Python services.",
                "additionalPlain": "Work with a distributed team.",
                "hostedUrl": "https://jobs.lever.co/leverexample/lever-202?lever-source=campaign",
                "applyUrl": "https://jobs.lever.co/leverexample/lever-202/apply",
                "workplaceType": "remote",
            }
        ]
        lever = job_sources.parse_lever_payload(lever_payload, "Lever Example")
        assert len(lever) == 1
        assert lever[0].location == "Remote United States"
        assert lever[0].apply_url.endswith("/apply")
        assert "distributed team" in lever[0].description
        assert job_sources.configured_company("https://jobs.lever.co/acme", ["Acme, Inc."]) == "Acme, Inc."

        old_fetch_json = job_sources.fetch_json

        def fake_fetch_json(url: str) -> object:
            if url.endswith("/v1/boards/acme"):
                return {"name": "Acme"}
            if "/v1/boards/acme/jobs" in url:
                return greenhouse_payload
            if "/v0/postings/leverexample" in url:
                return lever_payload
            raise AssertionError(f"Unexpected URL: {url}")

        try:
            job_sources.fetch_json = fake_fetch_json
            set_setting(
                "career_urls",
                "https://boards.greenhouse.io/acme\nhttps://jobs.lever.co/leverexample",
            )
            set_setting("target_companies", "Acme, Lever Example")
            set_setting("role_keywords", "engineer, Python")
            set_setting("locations", "remote, new york")
            set_setting("posted_within_days", "0")
            ats_first = jobs.discover_jobs()
            ats_second = jobs.discover_jobs()
        finally:
            job_sources.fetch_json = old_fetch_json

        assert ats_first["inserted"] == 2 and ats_first["filtered"] == 0, ats_first
        assert ats_second["seen"] == 2 and ats_second["inserted"] == 0, ats_second
        ats_jobs = rows("SELECT source, source_key, apply_url FROM jobs WHERE source IN ('greenhouse', 'lever')")
        assert len(ats_jobs) == 2
        assert all(job["source_key"] and job["apply_url"] for job in ats_jobs)

    print("job discovery ok")


if __name__ == "__main__":
    main()
