from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse


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

        from job_agent import broad_sources, job_sources, jobs
        from job_agent.db import init_db, rows, set_setting

        init_db()
        set_setting("discovery_providers", "")
        set_setting("career_stage_mode", "open")
        set_setting("target_companies", "ExampleCo")
        set_setting("target_company_mode", "only")
        set_setting("role_keywords", "platform engineer, Python")
        set_setting("locations", "remote")
        set_setting("posted_within_days", "14")

        with running_server() as career_url:
            set_setting("career_urls", career_url)
            first = jobs.discover_jobs()
            second = jobs.discover_jobs()

        assert first == {
            "inserted": 1,
            "seen": 0,
            "filtered": 4,
            "errors": 0,
            "sources": 1,
            "skipped": 0,
        }, first
        assert second == {
            "inserted": 0,
            "seen": 1,
            "filtered": 4,
            "errors": 0,
            "sources": 1,
            "skipped": 0,
        }, second
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
        assert json.loads(generic["metadata"])["posted_at_precision"] == "exact"
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

        preferred_company = jobs.evaluate_posting(
            job_sources.JobPosting(
                title="Product Manager",
                company="OtherCo",
                url="https://example.test/jobs/product",
                description="Own the product roadmap.",
                location="Remote",
            ),
            {
                "career_stage_mode": "open",
                "role_keywords": "product manager",
                "target_companies": "ExampleCo",
                "target_company_mode": "prefer",
                "locations": "remote",
                "posted_within_days": "0",
            },
        )
        assert preferred_company.accepted
        assert "Preferred company" not in preferred_company.reasons
        unrestricted_location = jobs.evaluate_posting(
            job_sources.JobPosting(
                title="Product Manager",
                company="ExampleCo",
                url="https://example.test/jobs/product-anywhere",
                description="Own the product roadmap.",
                location="Singapore",
            ),
            {
                "career_stage_mode": "open",
                "role_keywords": "product manager",
                "target_companies": "",
                "locations": "",
                "posted_within_days": "0",
            },
        )
        assert unrestricted_location.accepted
        strict_company = jobs.evaluate_posting(
            job_sources.JobPosting(
                title="Product Manager",
                company="OtherCo",
                url="https://example.test/jobs/product-strict",
                description="Own the product roadmap.",
                location="Remote",
            ),
            {
                "career_stage_mode": "open",
                "role_keywords": "product manager",
                "target_companies": "ExampleCo",
                "target_company_mode": "only",
                "locations": "remote",
                "posted_within_days": "0",
            },
        )
        assert not strict_company.accepted

        now = datetime.now(timezone.utc)
        jobicy_payload = {
            "jobs": [
                {
                    "id": "jobicy-1",
                    "jobTitle": "Associate Product Manager",
                    "companyName": "Jobicy Example",
                    "url": "https://jobicy.com/jobs/jobicy-1",
                    "jobDescription": "<p>Own a product roadmap and customer research.</p>",
                    "jobGeo": "United States",
                    "pubDate": now.isoformat(),
                }
            ]
        }
        remotive_payload = {
            "jobs": [
                {
                    "id": 201,
                    "title": "Junior Project Manager",
                    "company_name": "Remotive Example",
                    "url": "https://remotive.com/remote-jobs/project-management/remotive-1",
                    "description": "<p>Coordinate project delivery and stakeholders.</p>",
                    "candidate_required_location": "Worldwide",
                    "publication_date": now.isoformat(),
                }
            ]
        }
        arbeitnow_payload = {
            "data": [
                {
                    "slug": "arbeitnow-1",
                    "title": "Business Analyst",
                    "company_name": "Arbeitnow Example",
                    "url": "https://www.arbeitnow.com/jobs/arbeitnow-1",
                    "description": "<p>Analyze operations and delivery metrics.</p>",
                    "location": "United States",
                    "remote": True,
                    "created_at": int(now.timestamp()),
                }
            ]
        }
        wwr_feed = f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel><item>
          <title>WWR Example: Operations Manager</title>
          <link>https://weworkremotely.com/remote-jobs/wwr-example-operations-manager</link>
          <guid>wwr-1</guid>
          <region>Anywhere in the World</region>
          <pubDate>{format_datetime(now)}</pubDate>
          <description><![CDATA[<p>Improve business operations and processes.</p>]]></description>
        </item></channel></rss>"""

        assert broad_sources.parse_jobicy_payload(jobicy_payload)[0].workplace_type == "remote"
        assert broad_sources.parse_remotive_payload(remotive_payload)[0].source == "remotive"
        assert broad_sources.parse_arbeitnow_payload(arbeitnow_payload)[0].company == "Arbeitnow Example"
        assert broad_sources.parse_weworkremotely_feed(wwr_feed)[0].company == "WWR Example"

        old_broad_fetch_json = broad_sources.fetch_json
        old_broad_fetch_url = broad_sources.fetch_url

        def fake_broad_fetch_json(url: str) -> object:
            if url.startswith(broad_sources.PROVIDERS["jobicy"].source_url):
                return jobicy_payload
            if url.startswith(broad_sources.PROVIDERS["remotive"].source_url):
                return remotive_payload
            if url == broad_sources.PROVIDERS["arbeitnow"].source_url:
                return arbeitnow_payload
            raise AssertionError(f"Unexpected broad provider URL: {url}")

        def fake_broad_fetch_url(url: str) -> str:
            if url == broad_sources.PROVIDERS["weworkremotely"].source_url:
                return wwr_feed
            raise AssertionError(f"Unexpected broad provider URL: {url}")

        try:
            broad_sources.fetch_json = fake_broad_fetch_json
            broad_sources.fetch_url = fake_broad_fetch_url
            set_setting("career_urls", "")
            set_setting(
                "discovery_providers",
                "jobicy,remotive,weworkremotely,arbeitnow",
            )
            set_setting("target_companies", "PreferredCo")
            set_setting("target_company_mode", "prefer")
            set_setting(
                "role_keywords",
                "product manager, project manager, business analyst, operations manager",
            )
            set_setting("locations", "remote")
            set_setting("posted_within_days", "14")
            broad_first = jobs.discover_jobs()
            broad_second = jobs.discover_jobs()
            broad_forced = jobs.discover_jobs(force=True)
        finally:
            broad_sources.fetch_json = old_broad_fetch_json
            broad_sources.fetch_url = old_broad_fetch_url
            set_setting("discovery_providers", "")
            set_setting("target_company_mode", "only")

        assert broad_first == {
            "inserted": 4,
            "seen": 0,
            "filtered": 0,
            "errors": 0,
            "sources": 4,
            "skipped": 0,
        }, broad_first
        assert broad_second == {
            "inserted": 0,
            "seen": 0,
            "filtered": 0,
            "errors": 0,
            "sources": 4,
            "skipped": 4,
        }, broad_second
        assert broad_forced == {
            "inserted": 0,
            "seen": 4,
            "filtered": 0,
            "errors": 0,
            "sources": 4,
            "skipped": 0,
        }, broad_forced
        broad_states = {
            state["source_kind"]: state
            for state in jobs.list_source_states()
            if state["source_kind"] in broad_sources.PROVIDERS
        }
        assert set(broad_states) == set(broad_sources.PROVIDERS)
        assert all(state["scan_count"] == 2 for state in broad_states.values())
        assert all(state["metadata"]["attribution_url"] for state in broad_states.values())

        age_filter_base = {
            "role_keywords": "Python",
            "target_companies": "ExampleCo",
            "locations": "remote",
            "include_unknown_posted_at": "false",
        }
        exact_recent = job_sources.JobPosting(
            title="Python Engineer",
            company="ExampleCo",
            url="https://example.test/jobs/recent-exact",
            description="Build Python services.",
            location="Remote",
            posted_at=(datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(),
            metadata={"posted_at_precision": "exact"},
        )
        strict_hour = jobs.evaluate_posting(
            exact_recent,
            {
                **age_filter_base,
                "posted_age_mode": "hours",
                "posted_within_hours": "1",
            },
        )
        two_hours = jobs.evaluate_posting(
            exact_recent,
            {
                **age_filter_base,
                "posted_age_mode": "hours",
                "posted_within_hours": "2",
            },
        )
        assert not strict_hour.accepted and "hour old" in strict_hour.rejection
        assert two_hours.accepted and any("1h ago" in reason for reason in two_hours.reasons)

        date_only = job_sources.JobPosting(
            title="Python Engineer",
            company="ExampleCo",
            url="https://example.test/jobs/date-only",
            description="Build Python services.",
            location="Remote",
            posted_at=(datetime.now().astimezone() - timedelta(days=1)).date().isoformat(),
            metadata={"posted_at_precision": "date"},
        )
        hours_reject_date = jobs.evaluate_posting(
            date_only,
            {
                **age_filter_base,
                "posted_age_mode": "hours",
                "posted_within_hours": "24",
            },
        )
        days_include_date = jobs.evaluate_posting(
            date_only,
            {
                **age_filter_base,
                "posted_age_mode": "days",
                "posted_within_days": "1",
            },
        )
        assert not hours_reject_date.accepted
        assert "no exact time" in hours_reject_date.rejection
        assert days_include_date.accepted
        assert any("date only" in reason for reason in days_include_date.reasons)

        unknown_date = job_sources.JobPosting(
            title="Python Engineer",
            company="ExampleCo",
            url="https://example.test/jobs/unknown-date",
            description="Build Python services.",
            location="Remote",
        )
        unknown_excluded = jobs.evaluate_posting(
            unknown_date,
            {
                **age_filter_base,
                "posted_age_mode": "days",
                "posted_within_days": "1",
            },
        )
        unknown_included = jobs.evaluate_posting(
            unknown_date,
            {
                **age_filter_base,
                "posted_age_mode": "days",
                "posted_within_days": "1",
                "include_unknown_posted_at": "true",
            },
        )
        assert not unknown_excluded.accepted
        assert unknown_included.accepted
        assert job_sources.posting_timestamp("2026-07-27")[1] == "date"
        assert job_sources.posting_timestamp("2026-07-27T12:30:00Z")[1] == "exact"

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
        assert greenhouse[0].metadata["posted_at_precision"] == "exact"
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

        assert job_sources.source_kind("https://jobs.ashbyhq.com/AshbyExample") == "ashby"
        assert (
            job_sources.source_kind("https://jobs.smartrecruiters.com/SmartExample")
            == "smartrecruiters"
        )
        assert (
            job_sources.source_kind(
                "https://example.wd5.myworkdayjobs.com/en-US/External"
            )
            == "workday"
        )
        assert job_sources.ashby_board_name(
            "https://api.ashbyhq.com/posting-api/job-board/AshbyExample"
        ) == "AshbyExample"
        assert job_sources.smartrecruiters_company(
            "https://api.smartrecruiters.com/v1/companies/SmartExample/postings"
        ) == "SmartExample"
        assert job_sources.workday_site(
            "https://example.wd5.myworkdayjobs.com/en-US/External"
        ) == ("example", "External", "example.wd5.myworkdayjobs.com")
        assert job_sources.workday_site(
            "https://example.wd5.myworkdaysite.com/recruiting/example/External"
        ) == ("example", "External", "example.wd5.myworkdaysite.com")

        ashby_payload = {
            "jobs": [
                {
                    "id": f"ashby-{index}",
                    "title": f"Platform Engineer {index}",
                    "companyName": "Ashby Example",
                    "location": "United States",
                    "workplaceType": "Remote",
                    "descriptionPlain": f"Build Python platform services for team {index}.",
                    "publishedAt": now,
                    "jobUrl": (
                        f"https://jobs.ashbyhq.com/AshbyExample/ashby-{index}"
                        "?utm_source=fixture"
                    ),
                    "applyUrl": (
                        f"https://jobs.ashbyhq.com/AshbyExample/ashby-{index}/application"
                        "?utm_campaign=fixture"
                    ),
                    "isListed": True,
                }
                for index in range(5)
            ]
        }
        smart_items = [
            {
                "id": f"smart-{index}",
                "name": f"Platform Engineer {index}",
            }
            for index in range(5)
        ]
        smart_details = {
            item["id"]: {
                **item,
                "uuid": f"smart-uuid-{index}",
                "company": {"name": "Smart Example"},
                "location": {
                    "city": "New York",
                    "region": "NY",
                    "country": "us",
                    "remote": True,
                },
                "remote": True,
                "releasedDate": now,
                "applyUrl": (
                    "https://jobs.smartrecruiters.com/SmartExample/"
                    f"{item['id']}-platform-engineer?source=fixture"
                ),
                "jobAd": {
                    "sections": {
                        "jobDescription": {
                            "text": f"<p>Build Python APIs for platform team {index}.</p>"
                        },
                        "qualifications": {"text": "<p>Production engineering experience.</p>"},
                    }
                },
            }
            for index, item in enumerate(smart_items)
        }
        workday_items = [
            {
                "title": f"Platform Engineer {index}",
                "externalPath": f"/job/Remote/Platform-Engineer-{index}_WD-{index}",
                "locationsText": "Remote",
                "timeType": "Full time",
                "bulletFields": [f"WD-{index}"],
            }
            for index in range(5)
        ]
        workday_details = {
            item["externalPath"]: {
                "jobPostingInfo": {
                    "id": f"workday-{index}",
                    "title": item["title"],
                    "jobDescription": (
                        f"<p>Build Python infrastructure for platform team {index}.</p>"
                    ),
                    "location": "Remote",
                    "remoteType": "Remote",
                    "startDate": now,
                    "timeType": "Full time",
                    "jobReqId": f"WD-{index}",
                    "jobPostingSiteId": "External",
                    "externalUrl": (
                        "https://example.wd5.myworkdayjobs.com/en-US/External"
                        f"{item['externalPath']}"
                    ),
                },
                "hiringOrganization": {"name": "Workday Example"},
            }
            for index, item in enumerate(workday_items)
        }

        old_fetch_json = job_sources.fetch_json
        old_post_json = job_sources.post_json

        def fake_native_fetch_json(url: str) -> object:
            parsed = urlparse(url)
            if parsed.path == "/posting-api/job-board/AshbyExample":
                return ashby_payload
            if parsed.path == "/v1/companies/SmartExample/postings":
                query = parse_qs(parsed.query)
                offset = int(query.get("offset", ["0"])[0])
                requested = int(query.get("limit", ["100"])[0])
                page = smart_items[offset : offset + min(2, requested)]
                return {
                    "content": page,
                    "totalFound": len(smart_items),
                    "offset": offset,
                    "limit": requested,
                }
            smart_prefix = "/v1/companies/SmartExample/postings/"
            if parsed.path.startswith(smart_prefix):
                posting_id = parsed.path.removeprefix(smart_prefix)
                return smart_details[posting_id]
            workday_prefix = "/wday/cxs/example/External"
            if parsed.path.startswith(workday_prefix):
                external_path = parsed.path.removeprefix(workday_prefix)
                return workday_details[external_path]
            raise AssertionError(f"Unexpected native GET URL: {url}")

        def fake_native_post_json(url: str, payload: dict[str, object]) -> object:
            parsed = urlparse(url)
            assert parsed.path == "/wday/cxs/example/External/jobs", url
            offset = int(payload["offset"])
            requested = int(payload["limit"])
            page = workday_items[offset : offset + min(2, requested)]
            return {"jobPostings": page, "total": len(workday_items)}

        try:
            job_sources.fetch_json = fake_native_fetch_json
            job_sources.post_json = fake_native_post_json
            set_setting(
                "career_urls",
                "\n".join(
                    (
                        "https://jobs.ashbyhq.com/AshbyExample",
                        "https://jobs.smartrecruiters.com/SmartExample",
                        "https://example.wd5.myworkdayjobs.com/en-US/External",
                    )
                ),
            )
            set_setting(
                "target_companies",
                "Ashby Example, Smart Example, Workday Example",
            )
            set_setting("role_keywords", "engineer, Python")
            set_setting("locations", "remote")
            set_setting("posted_within_days", "0")
            set_setting("max_jobs_per_source", "3")
            native_first = jobs.discover_jobs()
            first_states = {
                state["source_kind"]: state for state in jobs.list_source_states()
                if state["source_kind"] in {"ashby", "smartrecruiters", "workday"}
            }
            native_second = jobs.discover_jobs()
        finally:
            job_sources.fetch_json = old_fetch_json
            job_sources.post_json = old_post_json

        assert native_first == {
            "inserted": 9,
            "seen": 0,
            "filtered": 0,
            "errors": 0,
            "sources": 3,
            "skipped": 0,
        }, native_first
        assert native_second == {
            "inserted": 6,
            "seen": 0,
            "filtered": 0,
            "errors": 0,
            "sources": 3,
            "skipped": 0,
        }, native_second
        assert set(first_states) == {"ashby", "smartrecruiters", "workday"}
        assert all(state["cursor"] == "3" for state in first_states.values()), first_states
        assert first_states["ashby"]["pages_scanned"] == 1
        assert first_states["smartrecruiters"]["pages_scanned"] == 2
        assert first_states["workday"]["pages_scanned"] == 2

        native_states = {
            state["source_kind"]: state for state in jobs.list_source_states()
            if state["source_kind"] in {"ashby", "smartrecruiters", "workday"}
        }
        assert all(state["cursor"] == "0" for state in native_states.values()), native_states
        assert all(state["scan_count"] == 2 for state in native_states.values())
        assert all(state["status"] == "ready" for state in native_states.values())
        assert all(state["metadata"]["complete_cycle"] for state in native_states.values())
        assert all(state["metadata"]["total"] == 5 for state in native_states.values())

        native_jobs = rows(
            """
            SELECT source, source_key, url, apply_url, description
            FROM jobs
            WHERE source IN ('ashby', 'smartrecruiters', 'workday')
            """
        )
        assert len(native_jobs) == 15
        assert {job["source"] for job in native_jobs} == {
            "ashby",
            "smartrecruiters",
            "workday",
        }
        assert all(job["source_key"] and job["apply_url"] for job in native_jobs)
        assert all("utm_" not in job["url"] and "source=" not in job["url"] for job in native_jobs)
        assert all("Python" in job["description"] for job in native_jobs)

        decision_job_id = jobs.add_manual_job(
            {
                "title": "Strategy Analyst",
                "company": "Decision Example",
                "url": "https://example.test/jobs/decision",
                "description": "Entry-level strategy and operations role.",
            }
        )
        assert jobs.decide_job(decision_job_id, "maybe")["status"] == "maybe"
        assert jobs.decide_job(decision_job_id, "reconsider")["status"] == "new"
        assert jobs.decide_job(decision_job_id, "reject")["status"] == "rejected"

        us_job_id = jobs.add_manual_job(
            {
                "title": "Associate Product Manager",
                "company": "US Scope Example",
                "url": "https://example.test/jobs/us-scope",
                "description": "Entry-level product role with OPT support.",
                "location": "New York, NY",
            }
        )
        foreign_job_id = jobs.add_manual_job(
            {
                "title": "Junior Consultant",
                "company": "Foreign Scope Example",
                "url": "https://example.test/jobs/foreign-scope",
                "description": "Entry-level consulting role.",
                "location": "Munich",
            }
        )
        refreshed = jobs.refresh_saved_job_matches(
            {
                "career_stage_mode": "graduate",
                "target_role_families": "product,consulting",
                "graduate_include_internships": "true",
                "graduate_max_required_experience_years": "3",
                "excluded_title_terms": "",
                "locations": "United States",
                "work_authorization_mode": "cpt_opt_future_sponsorship",
                "sponsorship_unknown_handling": "review",
                "posted_within_days": "0",
            }
        )
        assert refreshed["checked"] >= 2
        scope_statuses = {
            item["id"]: item["status"]
            for item in rows(
                "SELECT id, status FROM jobs WHERE id IN (?, ?)",
                (us_job_id, foreign_job_id),
            )
        }
        assert scope_statuses[us_job_id] == "new"
        assert scope_statuses[foreign_job_id] == "filtered"

    print("job discovery ok")


if __name__ == "__main__":
    main()
