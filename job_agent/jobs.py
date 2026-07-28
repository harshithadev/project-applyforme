from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone

from .broad_sources import discover_provider, provider_for, provider_keys
from .db import all_settings, connect, log, now_iso, row, rows
from .job_sources import (
    JobPosting,
    SourceResult,
    canonicalize_url,
    discover_source,
    source_kind,
)
from .latex import keyword_score
from .role_matching import evaluate_graduate_role


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


def _posting_precision(posting: JobPosting) -> str:
    configured = str(posting.metadata.get("posted_at_precision") or "").casefold()
    if configured in {"exact", "date", "unknown"}:
        return configured
    if not posting.posted_at:
        return "unknown"
    return (
        "date"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", posting.posted_at.strip())
        else "exact"
    )


def _positive_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(str(value or default)))
    except ValueError:
        return default


def evaluate_posting(posting: JobPosting, settings: dict[str, str]) -> MatchDecision:
    keywords = split_csv(settings.get("role_keywords", ""))
    locations = split_csv(settings.get("locations", ""))
    companies = split_csv(settings.get("target_companies", ""))
    keyword_terms = [(keyword, _normalized(keyword)) for keyword in keywords if _normalized(keyword)]
    location_terms = [(location, _normalized(location)) for location in locations if _normalized(location)]
    company_terms = [(company, _normalized(company)) for company in companies if _normalized(company)]
    title_blob = _normalized(posting.title)
    role_blob = _normalized(f"{posting.title} {posting.description}")
    career_stage_mode = str(
        settings.get("career_stage_mode", "open") or "open"
    ).casefold()
    graduate_role = (
        evaluate_graduate_role(posting, settings)
        if career_stage_mode == "graduate"
        else None
    )
    if graduate_role and not graduate_role.accepted:
        return MatchDecision(False, 0, [], graduate_role.rejection)
    title_hits = [keyword for keyword, term in keyword_terms if term in title_blob]
    role_hits = [keyword for keyword, term in keyword_terms if term in role_blob]
    if not graduate_role and keyword_terms and not role_hits:
        return MatchDecision(False, 0, [], "role keywords did not match")

    company_blob = _normalized(posting.company)
    company_match = not company_terms or (
        bool(company_blob)
        and any(term in company_blob or company_blob in term for _, term in company_terms)
    )
    target_company_mode = str(
        settings.get("target_company_mode", "only") or "only"
    ).casefold()
    if target_company_mode == "only" and not company_match:
        return MatchDecision(False, 0, [], "company is outside the target list")

    location_blob = _normalized(f"{posting.location} {posting.workplace_type}")
    location_hits = [location for location, term in location_terms if term in location_blob]
    if location_terms and location_blob and not location_hits:
        return MatchDecision(False, 0, [], "location did not match")

    age_mode = str(settings.get("posted_age_mode", "days") or "days").casefold()
    if age_mode not in {"hours", "days"}:
        age_mode = "days"
    maximum_age = _positive_int(
        settings.get(
            "posted_within_hours" if age_mode == "hours" else "posted_within_days",
            "0",
        )
    )
    include_unknown = (
        str(settings.get("include_unknown_posted_at", "true")).casefold() == "true"
    )
    posted = _timestamp(posting.posted_at)
    precision = _posting_precision(posting)
    age_value: int | None = None
    age_reason = ""
    if posted:
        current = datetime.now(timezone.utc)
        if age_mode == "hours":
            if maximum_age and precision != "exact":
                return MatchDecision(
                    False,
                    0,
                    [],
                    "posting has a date but no exact time",
                )
            elapsed_seconds = max(0, (current - posted).total_seconds())
            age_value = int(elapsed_seconds // 3_600)
            if maximum_age and elapsed_seconds > maximum_age * 3_600:
                unit = "hour" if age_value == 1 else "hours"
                return MatchDecision(False, 0, [], f"posting is {age_value} {unit} old")
            age_reason = f"Posted/updated {age_value}h ago"
        else:
            local_now = datetime.now().astimezone()
            posted_date = (
                posted.date()
                if precision == "date"
                else posted.astimezone(local_now.tzinfo).date()
            )
            age_value = max(0, (local_now.date() - posted_date).days)
            if maximum_age and age_value > maximum_age:
                unit = "day" if age_value == 1 else "days"
                return MatchDecision(
                    False,
                    0,
                    [],
                    f"posting is {age_value} calendar {unit} old",
                )
            date_only = " (date only)" if precision == "date" else ""
            age_reason = f"Posted/updated {age_value}d ago{date_only}"
    elif maximum_age and not include_unknown:
        return MatchDecision(False, 0, [], "posting date is not listed")

    role_ratio = len(role_hits) / max(len(keyword_terms), 1) if keyword_terms else 1
    title_ratio = len(title_hits) / max(len(keyword_terms), 1) if keyword_terms else 1
    score = (
        int(graduate_role.score)
        if graduate_role
        else round(role_ratio * 60 + title_ratio * 20)
    )
    score += 10 if location_hits else 6 if not location_terms else 4 if not location_blob else 0
    score += 5 if company_terms and company_match else 3 if not company_terms else 0
    score += 5 if age_value is not None else 2
    reasons = []
    if graduate_role:
        reasons.extend(graduate_role.reasons)
    elif role_hits:
        reasons.append(f"Role: {', '.join(role_hits[:4])}")
    if location_hits:
        reasons.append(f"Location: {', '.join(location_hits[:3])}")
    elif not location_blob:
        reasons.append("Location not listed")
    if company_terms and company_match:
        reasons.append("Preferred company")
    if age_reason:
        reasons.append(age_reason)
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


def _begin_source_scan(url: str, source_kind_override: str = "") -> dict[str, object]:
    source_url = canonicalize_url(url)
    kind = source_kind_override or source_kind(source_url)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO job_source_states(
              source_url, source_kind, status, scan_count, created_at, updated_at,
              last_scanned_at
            )
            VALUES(?, ?, 'running', 1, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
              source_kind = excluded.source_kind,
              status = 'running',
              scan_count = job_source_states.scan_count + 1,
              last_error = '',
              updated_at = excluded.updated_at,
              last_scanned_at = excluded.last_scanned_at
            """,
            (source_url, kind, now, now, now),
        )
    return row("SELECT * FROM job_source_states WHERE source_url = ?", (source_url,)) or {
        "source_url": source_url,
        "source_kind": kind,
        "cursor": "",
    }


def _finish_source_scan(
    source_url: str,
    *,
    cursor: str,
    pages_scanned: int,
    jobs_seen: int,
    errors: list[str],
    metadata: dict[str, object],
    complete: bool,
) -> None:
    now = now_iso()
    status = "partial" if errors else "ready"
    state_metadata = {**metadata, "complete_cycle": complete}
    with connect() as conn:
        conn.execute(
            """
            UPDATE job_source_states
            SET cursor = ?, status = ?, pages_scanned = ?, jobs_seen = ?,
                last_error = ?, metadata = ?, updated_at = ?, last_success_at = ?
            WHERE source_url = ?
            """,
            (
                cursor,
                status,
                pages_scanned,
                jobs_seen,
                "\n".join(errors)[-4000:],
                json.dumps(state_metadata),
                now,
                now,
                source_url,
            ),
        )


def _fail_source_scan(source_url: str, error: Exception) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE job_source_states
            SET status = 'error', last_error = ?, updated_at = ?
            WHERE source_url = ?
            """,
            (str(error)[-4000:], now_iso(), source_url),
        )


def list_source_states() -> list[dict[str, object]]:
    states = rows(
        "SELECT * FROM job_source_states ORDER BY updated_at DESC, source_url"
    )
    for state in states:
        try:
            state["metadata"] = json.loads(str(state.get("metadata") or "{}"))
        except json.JSONDecodeError:
            state["metadata"] = {}
    return states


def _configured_provider_keys(settings: dict[str, str]) -> list[str]:
    supported = set(provider_keys())
    configured: list[str] = []
    for key in split_csv(settings.get("discovery_providers", "")):
        normalized = key.casefold()
        if normalized in supported and normalized not in configured:
            configured.append(normalized)
    return configured


def _provider_cooldown_minutes(key: str) -> int:
    provider = provider_for(key)
    state = row(
        "SELECT last_success_at FROM job_source_states WHERE source_url = ?",
        (canonicalize_url(provider.source_url),),
    )
    last_success = _timestamp(str(state.get("last_success_at") or "")) if state else None
    if not last_success:
        return 0
    elapsed = (datetime.now(timezone.utc) - last_success).total_seconds() / 60
    return max(0, math.ceil(provider.minimum_interval_minutes - elapsed))


def _process_source_result(
    source_url: str,
    source_result: SourceResult,
    settings: dict[str, str],
    result: dict[str, int],
) -> None:
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
    _finish_source_scan(
        source_url,
        cursor=source_result.next_cursor,
        pages_scanned=source_result.pages_scanned,
        jobs_seen=len(source_result.postings),
        errors=source_result.errors,
        metadata=source_result.metadata,
        complete=source_result.complete,
    )


def discover_jobs() -> dict[str, int]:
    settings = all_settings()
    urls = split_csv(settings.get("career_urls", ""))
    providers = _configured_provider_keys(settings)
    companies = split_csv(settings.get("target_companies", ""))
    keywords = split_csv(settings.get("role_keywords", ""))
    try:
        limit = min(200, max(1, int(settings.get("max_jobs_per_source", "80") or "80")))
    except ValueError:
        limit = 80
    result = {
        "inserted": 0,
        "seen": 0,
        "filtered": 0,
        "errors": 0,
        "sources": len(providers) + len(urls),
        "skipped": 0,
    }
    if not providers and not urls:
        log("No discovery providers or company career URLs are enabled.", "warning")
        return result

    for key in providers:
        provider = provider_for(key)
        remaining = _provider_cooldown_minutes(key)
        if remaining:
            result["skipped"] += 1
            log(
                f"{provider.label} scan skipped for {remaining} more minute(s) "
                "to respect its polling interval."
            )
            continue
        state = _begin_source_scan(provider.source_url, provider.key)
        source_url = str(state["source_url"])
        try:
            source_result = discover_provider(key, limit)
        except Exception as exc:
            _fail_source_scan(source_url, exc)
            result["errors"] += 1
            log(
                f"Could not scan {provider.label}.",
                "error",
                {"error": str(exc)},
            )
            continue
        _process_source_result(source_url, source_result, settings, result)

    for url in urls:
        state = _begin_source_scan(url)
        source_url = str(state["source_url"])
        try:
            source_result = discover_source(
                url,
                companies,
                keywords,
                limit,
                str(state.get("cursor") or ""),
            )
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            _fail_source_scan(source_url, exc)
            result["errors"] += 1
            log(f"Could not scan {url}.", "error", {"error": str(exc)})
            continue
        _process_source_result(source_url, source_result, settings, result)
    log(
        "Job discovery complete: "
        f"{result['inserted']} new, {result['seen']} refreshed, "
        f"{result['filtered']} filtered, {result['errors']} error(s), "
        f"{result['skipped']} rate-limited source(s)."
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
