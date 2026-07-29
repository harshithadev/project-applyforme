from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit

from .job_sources import (
    JobPosting,
    SourceResult,
    canonicalize_url,
    clean_text,
    fetch_json,
    fetch_url,
    html_to_text,
    posting_timestamp,
)


@dataclass(frozen=True)
class DiscoveryProvider:
    key: str
    label: str
    source_url: str
    attribution_url: str
    minimum_interval_minutes: int


PROVIDERS = {
    "jobicy": DiscoveryProvider(
        key="jobicy",
        label="Jobicy",
        source_url="https://jobicy.com/api/v2/remote-jobs",
        attribution_url="https://jobicy.com/",
        minimum_interval_minutes=60,
    ),
    "remotive": DiscoveryProvider(
        key="remotive",
        label="Remotive",
        source_url="https://remotive.com/api/remote-jobs",
        attribution_url="https://remotive.com/",
        minimum_interval_minutes=360,
    ),
    "weworkremotely": DiscoveryProvider(
        key="weworkremotely",
        label="We Work Remotely",
        source_url="https://weworkremotely.com/remote-jobs.rss",
        attribution_url="https://weworkremotely.com/",
        minimum_interval_minutes=60,
    ),
    "arbeitnow": DiscoveryProvider(
        key="arbeitnow",
        label="Arbeitnow",
        source_url="https://www.arbeitnow.com/api/job-board-api",
        attribution_url="https://www.arbeitnow.com/",
        minimum_interval_minutes=60,
    ),
    "himalayas": DiscoveryProvider(
        key="himalayas",
        label="Himalayas",
        source_url="https://himalayas.app/jobs/api/search",
        attribution_url="https://himalayas.app/jobs",
        minimum_interval_minutes=1440,
    ),
}


def provider_keys() -> list[str]:
    return list(PROVIDERS)


def provider_for(key: str) -> DiscoveryProvider:
    try:
        return PROVIDERS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown discovery provider: {key}") from exc


def _external_id(item: dict[str, object], url: str) -> str:
    value = clean_text(item.get("id") or item.get("slug") or item.get("jobSlug"))
    if value:
        return value
    return urlsplit(url).path.rstrip("/").split("/")[-1]


def parse_jobicy_payload(payload: object) -> list[JobPosting]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("jobTitle") or item.get("title"))
        url = canonicalize_url(str(item.get("url") or item.get("jobUrl") or ""))
        if not title or not url:
            continue
        location = clean_text(item.get("jobGeo") or item.get("location") or "Remote")
        if "remote" not in location.casefold():
            location = clean_text(f"Remote - {location}")
        posted_at, precision = posting_timestamp(item.get("pubDate") or item.get("publishedAt"))
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(item.get("companyName") or item.get("company")),
                url=url,
                description=html_to_text(
                    item.get("jobDescription") or item.get("jobExcerpt") or ""
                ),
                location=location,
                source="jobicy",
                posted_at=posted_at,
                external_id=_external_id(item, url),
                apply_url=url,
                workplace_type="remote",
                metadata={
                    "industries": item.get("jobIndustry") or [],
                    "employment_types": item.get("jobType") or [],
                    "level": item.get("jobLevel") or "",
                    "posted_at_precision": precision,
                    "attribution_url": PROVIDERS["jobicy"].attribution_url,
                },
            )
        )
    return postings


def parse_remotive_payload(payload: object) -> list[JobPosting]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        url = canonicalize_url(str(item.get("url") or ""))
        if not title or not url:
            continue
        location = clean_text(item.get("candidate_required_location") or "Remote")
        if "remote" not in location.casefold():
            location = clean_text(f"Remote - {location}")
        posted_at, precision = posting_timestamp(item.get("publication_date"))
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(item.get("company_name")),
                url=url,
                description=html_to_text(item.get("description")),
                location=location,
                source="remotive",
                posted_at=posted_at,
                external_id=_external_id(item, url),
                apply_url=url,
                workplace_type="remote",
                metadata={
                    "category": item.get("category") or "",
                    "employment_type": item.get("job_type") or "",
                    "salary": item.get("salary") or "",
                    "posted_at_precision": precision,
                    "attribution_url": PROVIDERS["remotive"].attribution_url,
                },
            )
        )
    return postings


def parse_arbeitnow_payload(payload: object) -> list[JobPosting]:
    items = payload.get("data", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        url = canonicalize_url(str(item.get("url") or ""))
        if not title or not url:
            continue
        remote = bool(item.get("remote"))
        location = clean_text(item.get("location"))
        if remote and "remote" not in location.casefold():
            location = clean_text(f"Remote - {location}")
        posted_at, precision = posting_timestamp(
            item.get("created_at") or item.get("published_at")
        )
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(item.get("company_name") or item.get("company")),
                url=url,
                description=html_to_text(item.get("description")),
                location=location,
                source="arbeitnow",
                posted_at=posted_at,
                external_id=_external_id(item, url),
                apply_url=url,
                workplace_type="remote" if remote else "",
                metadata={
                    "tags": item.get("tags") or [],
                    "employment_types": item.get("job_types") or [],
                    "posted_at_precision": precision,
                    "attribution_url": PROVIDERS["arbeitnow"].attribution_url,
                },
            )
        )
    return postings


def _xml_text(item: ET.Element, name: str) -> str:
    for child in item.iter():
        if child.tag.rsplit("}", 1)[-1].casefold() == name.casefold():
            return clean_text(child.text)
    return ""


def _rss_timestamp(value: str) -> tuple[str, str]:
    if not value:
        return "", "unknown"
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return posting_timestamp(value)
    return posting_timestamp(parsed.isoformat())


def parse_weworkremotely_feed(body: str) -> list[JobPosting]:
    root = ET.fromstring(body)
    postings: list[JobPosting] = []
    for item in root.findall(".//item"):
        raw_title = _xml_text(item, "title")
        company = _xml_text(item, "company")
        title = raw_title
        if not company and ":" in raw_title:
            company, title = [clean_text(part) for part in raw_title.split(":", 1)]
        url = canonicalize_url(_xml_text(item, "link"))
        if not title or not url:
            continue
        location = _xml_text(item, "region") or _xml_text(item, "country") or "Remote"
        if "remote" not in location.casefold():
            location = clean_text(f"Remote - {location}")
        posted_at, precision = _rss_timestamp(_xml_text(item, "pubDate"))
        external_id = _xml_text(item, "guid") or urlsplit(url).path.rstrip("/").split("/")[-1]
        postings.append(
            JobPosting(
                title=title,
                company=company,
                url=url,
                description=html_to_text(_xml_text(item, "description")),
                location=location,
                source="weworkremotely",
                posted_at=posted_at,
                external_id=external_id,
                apply_url=url,
                workplace_type="remote",
                metadata={
                    "category": _xml_text(item, "category"),
                    "posted_at_precision": precision,
                    "attribution_url": PROVIDERS["weworkremotely"].attribution_url,
                },
            )
        )
    return postings


def _himalayas_location(item: dict[str, object]) -> str:
    raw_locations = item.get("locationRestrictions") or []
    locations: list[str] = []
    for raw in raw_locations if isinstance(raw_locations, list) else []:
        if isinstance(raw, dict):
            value = clean_text(raw.get("name") or raw.get("slug") or raw.get("alpha2"))
        else:
            value = clean_text(raw)
        if value and value not in locations:
            locations.append(value)
    return ", ".join(locations) or "Worldwide"


def parse_himalayas_payload(payload: object) -> list[JobPosting]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("title"))
        url = canonicalize_url(
            str(item.get("applicationLink") or item.get("guid") or "")
        )
        if not title or not url:
            continue
        posted_at, precision = posting_timestamp(item.get("pubDate"))
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(item.get("companyName")),
                url=url,
                description=html_to_text(
                    item.get("description") or item.get("excerpt") or ""
                ),
                location=_himalayas_location(item),
                source="himalayas",
                posted_at=posted_at,
                external_id=clean_text(item.get("guid")) or _external_id(item, url),
                apply_url=url,
                workplace_type="remote",
                metadata={
                    "employment_type": item.get("employmentType") or "",
                    "seniority": item.get("seniority") or [],
                    "categories": item.get("categories") or [],
                    "salary": item.get("salary") or "",
                    "posted_at_precision": precision,
                    "attribution_url": PROVIDERS["himalayas"].attribution_url,
                },
            )
        )
    return postings


def _himalayas_queries(settings: dict[str, str]) -> list[str]:
    query_by_family = {
        "product": "product manager",
        "project_program": "project program manager",
        "agile_delivery": "agile scrum",
        "consulting": "consultant",
        "change_transformation": "change transformation",
        "strategy_operations": "strategy operations",
    }
    selected = [
        part.strip()
        for part in str(settings.get("target_role_families") or "").split(",")
        if part.strip()
    ]
    queries = [
        query_by_family[key]
        for key in selected
        if key in query_by_family
    ]
    return queries or ["product project program consultant strategy operations"]


def _discover_himalayas(
    provider: DiscoveryProvider,
    selected_limit: int,
    settings: dict[str, str],
) -> SourceResult:
    postings: list[JobPosting] = []
    seen_urls: set[str] = set()
    queries = _himalayas_queries(settings)
    errors: list[str] = []
    for query in queries:
        query_url = f"{provider.source_url}?{urlencode({
            'q': query,
            'country': 'US',
            'sort': 'recent',
            'page': 1,
        })}"
        try:
            payload = fetch_json(query_url)
        except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"Himalayas query failed for {query}: {exc}")
            continue
        for posting in parse_himalayas_payload(payload):
            if posting.url in seen_urls:
                continue
            seen_urls.add(posting.url)
            postings.append(posting)
    postings.sort(key=lambda posting: posting.posted_at, reverse=True)
    return SourceResult(
        postings=postings[:selected_limit],
        errors=errors,
        complete=not errors,
        pages_scanned=len(queries),
        metadata={
            "provider": provider.key,
            "provider_label": provider.label,
            "total": len(postings),
            "queries": queries,
            "attribution_url": provider.attribution_url,
            "minimum_interval_minutes": provider.minimum_interval_minutes,
        },
    )


def discover_provider(
    key: str,
    limit: int,
    settings: dict[str, str] | None = None,
) -> SourceResult:
    provider = provider_for(key)
    selected_limit = min(100, max(1, limit))
    if key == "himalayas":
        return _discover_himalayas(provider, selected_limit, settings or {})
    if key == "jobicy":
        payload = fetch_json(
            f"{provider.source_url}?{urlencode({'count': selected_limit})}"
        )
        postings = parse_jobicy_payload(payload)
    elif key == "remotive":
        payload = fetch_json(
            f"{provider.source_url}?{urlencode({'limit': selected_limit})}"
        )
        postings = parse_remotive_payload(payload)
    elif key == "weworkremotely":
        postings = parse_weworkremotely_feed(fetch_url(provider.source_url))
    elif key == "arbeitnow":
        postings = parse_arbeitnow_payload(fetch_json(provider.source_url))
    else:
        raise ValueError(f"Unsupported discovery provider: {key}")
    return SourceResult(
        postings=postings[:selected_limit],
        complete=True,
        metadata={
            "provider": provider.key,
            "provider_label": provider.label,
            "total": len(postings),
            "attribution_url": provider.attribution_url,
            "minimum_interval_minutes": provider.minimum_interval_minutes,
        },
    )
