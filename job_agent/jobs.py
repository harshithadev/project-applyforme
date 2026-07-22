from __future__ import annotations

import hashlib
import json
import re
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import all_settings, connect, log, now_iso, rows
from .job_sources import JobPosting, canonicalize_url, discover_source
from .latex import keyword_score


@dataclass(frozen=True)
class MatchDecision:
    accepted: bool
    score: int
    reasons: list[str]
    rejection: str = ""


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n,]+", value) if part.strip()]


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9+#.]+", " ", value.lower()).strip()


def _timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def evaluate_posting(posting: JobPosting, settings: dict[str, str]) -> MatchDecision:
    keywords = split_csv(settings.get("role_keywords", ""))
    locations = split_csv(settings.get("locations", ""))
    companies = split_csv(settings.get("target_companies", ""))
    keyword_terms = [(keyword, _normalized(keyword)) for keyword in keywords if _normalized(keyword)]
    location_terms = [(location, _normalized(location)) for location in locations if _normalized(location)]
    company_terms = [(company, _normalized(company)) for company in companies if _normalized(company)]
    title_blob = _normalized(posting.title)
    role_blob = _normalized(f"{posting.title} {posting.description}")
    title_hits = [keyword for keyword, term in keyword_terms if term in title_blob]
    role_hits = [keyword for keyword, term in keyword_terms if term in role_blob]
    if keyword_terms and not role_hits:
        return MatchDecision(False, 0, [], "role keywords did not match")

    company_blob = _normalized(posting.company)
    company_match = not company_terms or (
        bool(company_blob)
        and any(term in company_blob or company_blob in term for _, term in company_terms)
    )
    if not company_match:
        return MatchDecision(False, 0, [], "company is outside the target list")

    location_blob = _normalized(f"{posting.location} {posting.workplace_type}")
    location_hits = [location for location, term in location_terms if term in location_blob]
    if location_terms and location_blob and not location_hits:
        return MatchDecision(False, 0, [], "location did not match")

    try:
        maximum_age = max(0, int(settings.get("posted_within_days", "0") or "0"))
    except ValueError:
        maximum_age = 0
    posted = _timestamp(posting.posted_at)
    age_days: int | None = None
    if posted:
        age_days = max(0, (datetime.now(timezone.utc) - posted).days)
        if maximum_age and age_days > maximum_age:
            return MatchDecision(False, 0, [], f"posting is {age_days} days old")

    role_ratio = len(role_hits) / max(len(keyword_terms), 1) if keyword_terms else 1
    title_ratio = len(title_hits) / max(len(keyword_terms), 1) if keyword_terms else 1
    score = round(role_ratio * 60 + title_ratio * 20)
    score += 10 if location_hits else 6 if not location_terms else 4 if not location_blob else 0
    score += 5 if company_terms else 3
    score += 5 if age_days is not None and (not maximum_age or age_days <= maximum_age) else 2
    reasons = []
    if role_hits:
        reasons.append(f"Role: {', '.join(role_hits[:4])}")
    if location_hits:
        reasons.append(f"Location: {', '.join(location_hits[:3])}")
    elif not location_blob:
        reasons.append("Location not listed")
    if company_terms:
        reasons.append("Target company")
    if age_days is not None:
        reasons.append(f"Posted/updated {age_days}d ago")
    else:
        reasons.append("Posting date not listed")
    return MatchDecision(True, min(100, score), reasons)


def _source_key(posting: JobPosting) -> str:
    return f"{posting.source}:{posting.external_id}" if posting.external_id else ""


def _fingerprint(posting: JobPosting) -> str:
    value = "|".join(
        _normalized(item)
        for item in (posting.company, posting.title, posting.location, posting.external_id)
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _persist_posting(posting: JobPosting, decision: MatchDecision) -> bool:
    now = now_iso()
    url = canonicalize_url(posting.url)
    source_key = _source_key(posting)
    fingerprint = _fingerprint(posting)
    with connect() as conn:
        if source_key:
            existing = conn.execute(
                "SELECT id FROM jobs WHERE url = ? OR source_key = ? LIMIT 1",
                (url, source_key),
            ).fetchone()
        else:
            existing = conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone()
        values = (
            posting.title[:180],
            posting.company[:120],
            url,
            posting.description,
            posting.location[:240],
            posting.source,
            decision.score,
            posting.posted_at or None,
            posting.external_id,
            source_key,
            fingerprint,
            posting.apply_url,
            posting.workplace_type,
            json.dumps(decision.reasons),
            json.dumps(posting.metadata),
            now,
            now,
        )
        if existing:
            conn.execute(
                """
                UPDATE jobs
                SET title = ?, company = ?, url = ?, description = ?, location = ?, source = ?,
                    score = ?, posted_at = ?, external_id = ?, source_key = ?, fingerprint = ?,
                    apply_url = ?, workplace_type = ?, match_reasons = ?, metadata = ?,
                    last_seen_at = ?, description_fetched_at = ?, updated_at = ?
                WHERE id = ?
                """,
                values + (now, int(existing["id"])),
            )
            return False
        conn.execute(
            """
            INSERT INTO jobs(
                title, company, url, description, location, source, score, posted_at,
                external_id, source_key, fingerprint, apply_url, workplace_type,
                match_reasons, metadata, discovered_at, last_seen_at,
                description_fetched_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values[:15] + (now, now, now, now),
        )
    return True


def discover_jobs() -> dict[str, int]:
    settings = all_settings()
    urls = split_csv(settings.get("career_urls", ""))
    companies = split_csv(settings.get("target_companies", ""))
    keywords = split_csv(settings.get("role_keywords", ""))
    try:
        limit = min(200, max(1, int(settings.get("max_jobs_per_source", "80") or "80")))
    except ValueError:
        limit = 80
    result = {"inserted": 0, "seen": 0, "filtered": 0, "errors": 0, "sources": len(urls)}
    if not urls:
        log("No career URLs configured. Add company career pages in Settings.", "warning")
        return result

    for url in urls:
        try:
            source_result = discover_source(url, companies, keywords, limit)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            result["errors"] += 1
            log(f"Could not scan {url}.", "error", {"error": str(exc)})
            continue
        result["errors"] += len(source_result.errors)
        for message in source_result.errors:
            log(message, "error")
        for posting in source_result.postings:
            decision = evaluate_posting(posting, settings)
            if not decision.accepted:
                result["filtered"] += 1
                continue
            if _persist_posting(posting, decision):
                result["inserted"] += 1
            else:
                result["seen"] += 1
    log(
        "Career scan complete: "
        f"{result['inserted']} new, {result['seen']} refreshed, "
        f"{result['filtered']} filtered, {result['errors']} error(s)."
    )
    return result


def add_manual_job(payload: dict[str, object]) -> int:
    title = str(payload.get("title") or "Untitled role").strip()
    company = str(payload.get("company") or "Unknown company").strip()
    url = canonicalize_url(str(payload.get("url") or f"manual:{company}:{title}:{now_iso()}").strip())
    description = str(payload.get("description") or "").strip()
    location = str(payload.get("location") or "").strip()
    keywords = split_csv(all_settings().get("role_keywords", ""))
    score = keyword_score(f"{title} {description}", keywords)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs(
                title, company, url, description, location, source, score,
                discovered_at, last_seen_at, description_fetched_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              title = excluded.title,
              company = excluded.company,
              description = excluded.description,
              location = excluded.location,
              score = excluded.score,
              last_seen_at = excluded.last_seen_at,
              description_fetched_at = excluded.description_fetched_at,
              updated_at = excluded.updated_at
            """,
            (title, company, url, description, location, "manual", score, now, now, now, now),
        )
        found = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    log(f"Saved job: {title} at {company}.")
    return int(found["id"])


def list_jobs() -> list[dict[str, object]]:
    jobs = rows("SELECT * FROM jobs ORDER BY discovered_at DESC, id DESC LIMIT 200")
    for job in jobs:
        try:
            job["match_reasons"] = json.loads(str(job.get("match_reasons") or "[]"))
        except json.JSONDecodeError:
            job["match_reasons"] = []
        try:
            job["metadata"] = json.loads(str(job.get("metadata") or "{}"))
        except json.JSONDecodeError:
            job["metadata"] = {}
    return jobs
