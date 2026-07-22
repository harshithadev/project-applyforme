from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .db import all_settings, connect, log, now_iso, rows
from .latex import keyword_score


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = " ".join(" ".join(self._text).split())
            self.links.append((self._href, text))
            self._href = ""
            self._text = []


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n,]+", value) if part.strip()]


def fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ApplyForMeLocal/0.1"})
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read(2_000_000)
    return raw.decode("utf-8", errors="replace")


def discover_jobs() -> dict[str, int]:
    settings = all_settings()
    urls = split_csv(settings.get("career_urls", ""))
    companies = split_csv(settings.get("target_companies", ""))
    keywords = split_csv(settings.get("role_keywords", ""))
    inserted = 0
    seen = 0
    if not urls:
        log("No career URLs configured. Add company career pages in Settings.", "warning")
        return {"inserted": 0, "seen": 0}

    with connect() as conn:
        for url in urls:
            try:
                body = fetch_url(url)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                log(f"Could not scan {url}.", "error", {"error": str(exc)})
                continue
            parser = LinkParser()
            parser.feed(body)
            base_host = urlparse(url).netloc
            candidates = []
            for href, text in parser.links:
                absolute = urljoin(url, href)
                blob = f"{text} {absolute}".lower()
                if any(keyword.lower() in blob for keyword in keywords) or any(token in blob for token in ("job", "career", "opening", "position")):
                    candidates.append((absolute, html.unescape(text or "Open role")))
            for job_url, title in candidates[:80]:
                company = next((company for company in companies if company.lower() in f"{url} {job_url}".lower()), base_host)
                desc = f"Discovered from {url}. Open the job URL to enrich the description before tailoring."
                score = keyword_score(f"{title} {desc}", keywords)
                now = now_iso()
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs(title, company, url, description, source, score, discovered_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (title[:180], company[:120], job_url, desc, "career-scan", score, now, now),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    seen += 1
    log(f"Career scan complete: {inserted} new job(s), {seen} already seen.")
    return {"inserted": inserted, "seen": seen}


def add_manual_job(payload: dict[str, object]) -> int:
    title = str(payload.get("title") or "Untitled role").strip()
    company = str(payload.get("company") or "Unknown company").strip()
    url = str(payload.get("url") or f"manual:{company}:{title}:{now_iso()}").strip()
    description = str(payload.get("description") or "").strip()
    location = str(payload.get("location") or "").strip()
    keywords = split_csv(all_settings().get("role_keywords", ""))
    score = keyword_score(f"{title} {description}", keywords)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs(title, company, url, description, location, source, score, discovered_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              title = excluded.title,
              company = excluded.company,
              description = excluded.description,
              location = excluded.location,
              score = excluded.score,
              updated_at = excluded.updated_at
            """,
            (title, company, url, description, location, "manual", score, now, now),
        )
        found = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    log(f"Saved job: {title} at {company}.")
    return int(found["id"])


def list_jobs() -> list[dict[str, object]]:
    return rows("SELECT * FROM jobs ORDER BY discovered_at DESC, id DESC LIMIT 200")
