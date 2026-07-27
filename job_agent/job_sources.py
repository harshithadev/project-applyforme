from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import ats_adapters


USER_AGENT = "ApplyForMeLocal/0.2"
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "gh_src",
    "lever-source",
    "ref",
    "referrer",
    "source",
}


@dataclass
class JobPosting:
    title: str
    company: str
    url: str
    description: str
    location: str = ""
    source: str = "career-detail"
    posted_at: str = ""
    external_id: str = ""
    apply_url: str = ""
    workplace_type: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class SourceResult:
    postings: list[JobPosting] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    next_cursor: str = ""
    pages_scanned: int = 1
    complete: bool = True
    metadata: dict[str, object] = field(default_factory=dict)


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
            text = clean_text(" ".join(self._text))
            self.links.append((self._href, text))
            self._href = ""
            self._text = []


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class JobPageParser(HTMLParser):
    SKIPPED = {"script", "style", "svg", "nav", "footer", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.meta_description = ""
        self.json_ld: list[str] = []
        self._capture_title = False
        self._capture_heading = False
        self._capture_json_ld = False
        self._json_parts: list[str] = []
        self._skipped_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if lower == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._capture_json_ld = True
            self._json_parts = []
            return
        if lower in self.SKIPPED:
            self._skipped_depth += 1
        if lower == "title":
            self._capture_title = True
        if lower == "h1":
            self._capture_heading = True
        if lower == "meta":
            key = (attributes.get("property") or attributes.get("name")).lower()
            if key in {"description", "og:description"} and attributes.get("content"):
                self.meta_description = attributes["content"]

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower == "script" and self._capture_json_ld:
            self.json_ld.append("".join(self._json_parts))
            self._capture_json_ld = False
            self._json_parts = []
            return
        if lower == "title":
            self._capture_title = False
        if lower == "h1":
            self._capture_heading = False
        if lower in self.SKIPPED and self._skipped_depth:
            self._skipped_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_json_ld:
            self._json_parts.append(data)
            return
        if self._capture_title:
            self.title_parts.append(data)
        if self._capture_heading:
            self.heading_parts.append(data)
        if not self._skipped_depth:
            self.visible_parts.append(data)


def clean_text(value: object) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def html_to_text(value: object) -> str:
    raw = html.unescape(html.unescape(str(value or "")))
    parser = TextParser()
    parser.feed(raw)
    return clean_text(" ".join(parser.parts))


def canonicalize_url(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return value
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    host = (parsed.hostname or "").lower()
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(sorted(query)), ""))


def normalize_timestamp(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            text = str(value).strip()
            if re.fullmatch(r"\d{10,13}", text):
                return normalize_timestamp(int(text))
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="seconds")
    except (OverflowError, TypeError, ValueError):
        return ""


def fetch_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/html,application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(4_000_000)
    return raw.decode("utf-8", errors="replace")


def fetch_json(url: str) -> object:
    return json.loads(fetch_url(url))


def post_json(url: str, payload: dict[str, object]) -> object:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(4_000_000)
    return json.loads(raw.decode("utf-8", errors="replace"))


def source_kind(url: str) -> str:
    return ats_adapters.source_kind(url)


def configured_company(url: str, companies: list[str]) -> str:
    normalized_url = re.sub(r"[^a-z0-9]", "", url.lower())
    path_tokens = {
        re.sub(r"[^a-z0-9]", "", part.lower())
        for part in urlsplit(url).path.split("/")
        if len(re.sub(r"[^a-z0-9]", "", part.lower())) >= 3
    }
    path_tokens -= {"apply", "boards", "jobs", "postings"}
    for company in companies:
        company_key = re.sub(r"[^a-z0-9]", "", company.lower())
        if company_key in normalized_url or any(token in company_key for token in path_tokens):
            return company
    if len(companies) == 1:
        return companies[0]
    host = (urlsplit(url).hostname or "Unknown company").lower()
    return host.removeprefix("www.")


def greenhouse_board_token(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname == "boards-api.greenhouse.io":
        try:
            return parts[parts.index("boards") + 1]
        except (ValueError, IndexError):
            return ""
    return parts[0] if parts else ""


def lever_site(url: str) -> tuple[str, bool]:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname in {"api.lever.co", "api.eu.lever.co"}:
        try:
            site = parts[parts.index("postings") + 1]
        except (ValueError, IndexError):
            site = ""
    else:
        site = parts[0] if parts else ""
    return site, ".eu.lever.co" in (parsed.hostname or "")


def ashby_board_name(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname == "api.ashbyhq.com":
        try:
            return parts[parts.index("job-board") + 1]
        except (ValueError, IndexError):
            return ""
    return parts[0] if parts else ""


def smartrecruiters_company(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname == "api.smartrecruiters.com":
        try:
            return parts[parts.index("companies") + 1]
        except (ValueError, IndexError):
            return ""
    return parts[0] if parts else ""


def workday_site(url: str) -> tuple[str, str, str]:
    parsed = urlsplit(url)
    host = parsed.netloc.lower()
    tenant = (parsed.hostname or "").split(".")[0]
    parts = [part for part in parsed.path.split("/") if part]
    if "wday" in parts and "cxs" in parts:
        try:
            index = parts.index("cxs")
            return parts[index + 1], parts[index + 2], host
        except IndexError:
            return "", "", host
    if len(parts) >= 3 and parts[0].lower() == "recruiting":
        return parts[1], parts[2], host
    if parts and re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[0], re.IGNORECASE):
        parts = parts[1:]
    site = parts[0] if parts else ""
    return tenant, site, host


def _offset_cursor(value: str) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError:
        return 0


def parse_greenhouse_payload(payload: object, company: str) -> list[JobPosting]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        location_data = item.get("location")
        location = clean_text(location_data.get("name")) if isinstance(location_data, dict) else ""
        url = canonicalize_url(str(item.get("absolute_url") or ""))
        title = clean_text(item.get("title"))
        if not title or not url:
            continue
        description = html_to_text(item.get("content"))
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(item.get("company_name")) or company,
                url=url,
                description=description,
                location=location,
                source="greenhouse",
                posted_at=normalize_timestamp(item.get("first_published") or item.get("updated_at")),
                external_id=str(item.get("id") or ""),
                apply_url=url,
                workplace_type="remote" if "remote" in location.lower() else "",
                metadata={"requisition_id": item.get("requisition_id")},
            )
        )
    return postings


def parse_lever_payload(payload: object, company: str) -> list[JobPosting]:
    items = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
        workplace_type = clean_text(item.get("workplaceType")).lower()
        location = clean_text(categories.get("location"))
        if workplace_type == "remote" and "remote" not in location.lower():
            location = clean_text(f"Remote {location}")
        description = clean_text(item.get("descriptionPlain")) or html_to_text(item.get("description"))
        additional = clean_text(item.get("additionalPlain")) or html_to_text(item.get("additional"))
        if additional and additional not in description:
            description = clean_text(f"{description} {additional}")
        url = canonicalize_url(str(item.get("hostedUrl") or ""))
        title = clean_text(item.get("text"))
        if not title or not url:
            continue
        postings.append(
            JobPosting(
                title=title,
                company=company,
                url=url,
                description=description,
                location=location,
                source="lever",
                posted_at=normalize_timestamp(item.get("createdAt")),
                external_id=str(item.get("id") or ""),
                apply_url=canonicalize_url(str(item.get("applyUrl") or url)),
                workplace_type=workplace_type,
                metadata={
                    "team": categories.get("team", ""),
                    "department": categories.get("department", ""),
                    "commitment": categories.get("commitment", ""),
                },
            )
        )
    return postings


def parse_ashby_payload(payload: object, company: str) -> list[JobPosting]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("isListed") is False:
            continue
        title = clean_text(item.get("title"))
        url = canonicalize_url(str(item.get("jobUrl") or ""))
        if not title or not url:
            continue
        workplace_type = clean_text(item.get("workplaceType")).lower()
        location = clean_text(item.get("location"))
        if workplace_type == "remote" and "remote" not in location.lower():
            location = clean_text(f"Remote {location}")
        external_id = clean_text(item.get("id"))
        if not external_id:
            external_id = urlsplit(url).path.rstrip("/").split("/")[-1]
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(item.get("companyName")) or company,
                url=url,
                description=clean_text(item.get("descriptionPlain"))
                or html_to_text(item.get("descriptionHtml")),
                location=location,
                source="ashby",
                posted_at=normalize_timestamp(item.get("publishedAt")),
                external_id=external_id,
                apply_url=canonicalize_url(str(item.get("applyUrl") or url)),
                workplace_type=workplace_type,
                metadata={
                    "department": item.get("department", ""),
                    "team": item.get("team", ""),
                    "employment_type": item.get("employmentType", ""),
                },
            )
        )
    return postings


def _smartrecruiters_description(item: dict[str, Any]) -> str:
    job_ad = item.get("jobAd") if isinstance(item.get("jobAd"), dict) else {}
    sections = job_ad.get("sections") if isinstance(job_ad.get("sections"), dict) else {}
    parts: list[str] = []
    for section in sections.values():
        if isinstance(section, dict):
            text = html_to_text(section.get("text") or section.get("html"))
        else:
            text = html_to_text(section)
        if text and text not in parts:
            parts.append(text)
    return clean_text(" ".join(parts)) or html_to_text(item.get("description"))


def _smartrecruiters_location(item: dict[str, Any]) -> str:
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    parts = [
        clean_text(location.get("city")),
        clean_text(location.get("region")),
        clean_text(location.get("country")),
    ]
    rendered = ", ".join(part for part in parts if part)
    remote = bool(item.get("remote") or location.get("remote"))
    if remote and "remote" not in rendered.lower():
        return clean_text(f"Remote {rendered}")
    return rendered


def parse_smartrecruiters_payload(
    payload: object,
    company: str,
    company_identifier: str,
) -> list[JobPosting]:
    items = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    postings: list[JobPosting] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = clean_text(item.get("name") or item.get("title"))
        posting_id = clean_text(item.get("id"))
        external_id = clean_text(item.get("uuid")) or posting_id
        public_url = str(item.get("applyUrl") or item.get("jobAdUrl") or item.get("postingUrl") or "")
        if not public_url and posting_id:
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            public_url = f"https://jobs.smartrecruiters.com/{company_identifier}/{posting_id}-{slug}"
        url = canonicalize_url(public_url)
        if not title or not url:
            continue
        company_data = item.get("company") if isinstance(item.get("company"), dict) else {}
        workplace_type = "remote" if bool(item.get("remote")) else ""
        postings.append(
            JobPosting(
                title=title,
                company=clean_text(company_data.get("name")) or company,
                url=url,
                description=_smartrecruiters_description(item),
                location=_smartrecruiters_location(item),
                source="smartrecruiters",
                posted_at=normalize_timestamp(item.get("releasedDate") or item.get("createdOn")),
                external_id=external_id,
                apply_url=canonicalize_url(str(item.get("applyUrl") or url)),
                workplace_type=workplace_type,
                metadata={
                    "posting_id": posting_id,
                    "uuid": item.get("uuid", ""),
                    "department": item.get("department", {}),
                    "employment_type": item.get("typeOfEmployment", {}),
                },
            )
        )
    return postings


def parse_workday_posting(
    item: dict[str, Any],
    detail_payload: object,
    company: str,
    public_base: str,
    site: str,
) -> JobPosting | None:
    detail = detail_payload if isinstance(detail_payload, dict) else {}
    info = detail.get("jobPostingInfo") if isinstance(detail.get("jobPostingInfo"), dict) else {}
    organization = (
        detail.get("hiringOrganization")
        if isinstance(detail.get("hiringOrganization"), dict)
        else {}
    )
    title = clean_text(info.get("title") or item.get("title"))
    external_path = str(item.get("externalPath") or "")
    public_url = str(info.get("externalUrl") or "")
    if not public_url and external_path:
        public_url = f"{public_base}/{site}{external_path}"
    url = canonicalize_url(public_url)
    if not title or not url:
        return None
    location = clean_text(info.get("location") or item.get("locationsText"))
    workplace_type = clean_text(info.get("remoteType") or item.get("remoteType")).lower()
    if workplace_type == "remote" and "remote" not in location.lower():
        location = clean_text(f"Remote {location}")
    bullet_fields = item.get("bulletFields") if isinstance(item.get("bulletFields"), list) else []
    external_id = clean_text(
        info.get("jobReqId")
        or info.get("id")
        or (bullet_fields[0] if bullet_fields else "")
        or urlsplit(url).path.rstrip("/").split("/")[-1]
    )
    return JobPosting(
        title=title,
        company=clean_text(organization.get("name")) or company,
        url=url,
        description=html_to_text(info.get("jobDescription")),
        location=location,
        source="workday",
        posted_at=normalize_timestamp(info.get("startDate")),
        external_id=external_id,
        apply_url=url,
        workplace_type=workplace_type,
        metadata={
            "time_type": info.get("timeType") or item.get("timeType", ""),
            "end_date": normalize_timestamp(info.get("endDate")),
            "posting_site": info.get("jobPostingSiteId") or site,
        },
    )


def _json_ld_objects(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            result.extend(_json_ld_objects(item))
        return result
    if not isinstance(value, dict):
        return []
    result = [value]
    if "@graph" in value:
        result.extend(_json_ld_objects(value["@graph"]))
    return result


def _is_job_posting(value: dict[str, Any]) -> bool:
    kind = value.get("@type", "")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(item).lower() == "jobposting" for item in kinds)


def _job_location(value: dict[str, Any]) -> str:
    if str(value.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        return "Remote"
    locations = value.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]
    rendered: list[str] = []
    for location in locations if isinstance(locations, list) else []:
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry", "")
        if isinstance(country, dict):
            country = country.get("name", "")
        parts = [
            clean_text(address.get("addressLocality")),
            clean_text(address.get("addressRegion")),
            clean_text(country),
        ]
        text = ", ".join(part for part in parts if part)
        if text and text not in rendered:
            rendered.append(text)
    return " / ".join(rendered)


def parse_job_page(body: str, url: str, fallback_title: str, fallback_company: str) -> JobPosting:
    parser = JobPageParser()
    parser.feed(body)
    structured: dict[str, Any] | None = None
    for raw in parser.json_ld:
        try:
            candidates = _json_ld_objects(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        structured = next((item for item in candidates if _is_job_posting(item)), None)
        if structured:
            break

    if structured:
        organization = structured.get("hiringOrganization")
        company = clean_text(organization.get("name")) if isinstance(organization, dict) else ""
        workplace_type = clean_text(structured.get("jobLocationType")).lower()
        return JobPosting(
            title=clean_text(structured.get("title")) or fallback_title,
            company=company or fallback_company,
            url=canonicalize_url(str(structured.get("url") or url)),
            description=html_to_text(structured.get("description")),
            location=_job_location(structured),
            source="career-detail",
            posted_at=normalize_timestamp(structured.get("datePosted")),
            external_id=clean_text(structured.get("identifier", {}).get("value"))
            if isinstance(structured.get("identifier"), dict)
            else "",
            apply_url=canonicalize_url(str(structured.get("url") or url)),
            workplace_type=workplace_type,
            metadata={"valid_through": normalize_timestamp(structured.get("validThrough"))},
        )

    heading = clean_text(" ".join(parser.heading_parts))
    page_title = clean_text(" ".join(parser.title_parts))
    description = clean_text(parser.meta_description)
    visible = clean_text(" ".join(parser.visible_parts))
    if len(visible) > len(description):
        description = visible
    return JobPosting(
        title=heading or fallback_title or page_title or "Open role",
        company=fallback_company,
        url=canonicalize_url(url),
        description=description,
        source="career-detail",
        apply_url=canonicalize_url(url),
    )


def _looks_like_job(href: str, text: str, keywords: list[str]) -> bool:
    parsed = urlsplit(href)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return False
    blob = f"{text} {parsed.path} {parsed.query}".lower()
    route_match = any(token in blob for token in ("job", "career", "opening", "position", "vacanc"))
    keyword_match = any(keyword.lower() in blob for keyword in keywords)
    return bool(text and (route_match or keyword_match))


def discover_generic(url: str, companies: list[str], keywords: list[str], limit: int) -> SourceResult:
    body = fetch_url(url)
    parser = LinkParser()
    parser.feed(body)
    fallback_company = configured_company(url, companies)
    candidates: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    for href, title in parser.links:
        absolute = canonicalize_url(urljoin(url, href))
        if not _looks_like_job(absolute, title, keywords) or absolute in seen_urls:
            continue
        seen_urls.add(absolute)
        candidates.append((absolute, title))
        if len(candidates) >= limit:
            break

    result = SourceResult()
    for job_url, title in candidates:
        try:
            detail = body if job_url == canonicalize_url(url) else fetch_url(job_url)
            result.postings.append(parse_job_page(detail, job_url, title, fallback_company))
        except Exception as exc:
            result.errors.append(f"Could not enrich {job_url}: {exc}")
    return result


def discover_greenhouse(url: str, companies: list[str]) -> SourceResult:
    token = greenhouse_board_token(url)
    if not token:
        raise ValueError("Greenhouse board URL does not contain a board token")
    company = configured_company(url, companies)
    try:
        board = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}")
        if isinstance(board, dict):
            company = clean_text(board.get("name")) or company
    except Exception:
        pass
    payload = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    return SourceResult(postings=parse_greenhouse_payload(payload, company))


def discover_lever(url: str, companies: list[str]) -> SourceResult:
    site, eu = lever_site(url)
    if not site:
        raise ValueError("Lever board URL does not contain a site name")
    company = configured_company(url, companies)
    host = "api.eu.lever.co" if eu else "api.lever.co"
    payload = fetch_json(f"https://{host}/v0/postings/{site}?mode=json")
    return SourceResult(postings=parse_lever_payload(payload, company))


def discover_ashby(
    url: str,
    companies: list[str],
    limit: int,
    cursor: str = "",
) -> SourceResult:
    board = ashby_board_name(url)
    if not board:
        raise ValueError("Ashby URL does not contain a job board name")
    company = configured_company(url, companies)
    payload = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    postings = parse_ashby_payload(payload, company)
    offset = _offset_cursor(cursor)
    selected = postings[offset : offset + limit]
    next_offset = offset + len(selected)
    complete = next_offset >= len(postings)
    return SourceResult(
        postings=selected,
        next_cursor="0" if complete else str(next_offset),
        pages_scanned=1,
        complete=complete,
        metadata={"total": len(postings), "board": board},
    )


def discover_smartrecruiters(
    url: str,
    companies: list[str],
    limit: int,
    cursor: str = "",
) -> SourceResult:
    company_identifier = smartrecruiters_company(url)
    if not company_identifier:
        raise ValueError("SmartRecruiters URL does not contain a company identifier")
    company = configured_company(url, companies)
    offset = _offset_cursor(cursor)
    postings: list[JobPosting] = []
    errors: list[str] = []
    pages = 0
    total = 0
    complete = False
    while len(postings) < limit:
        requested = min(100, limit - len(postings))
        query = urlencode({"limit": requested, "offset": offset})
        payload = fetch_json(
            f"https://api.smartrecruiters.com/v1/companies/{company_identifier}/postings?{query}"
        )
        if not isinstance(payload, dict):
            raise ValueError("SmartRecruiters postings response is not an object")
        pages += 1
        items = payload.get("content") if isinstance(payload.get("content"), list) else []
        total = int(payload.get("totalFound") or len(items))
        if not items:
            complete = True
            break
        detailed: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            posting_id = clean_text(item.get("id"))
            detail_url = str(item.get("ref") or "")
            if not detail_url and posting_id:
                detail_url = (
                    f"https://api.smartrecruiters.com/v1/companies/"
                    f"{company_identifier}/postings/{posting_id}"
                )
            detail: dict[str, Any] = {}
            if detail_url:
                try:
                    found = fetch_json(detail_url)
                    if isinstance(found, dict):
                        detail = found
                except Exception as exc:
                    errors.append(f"Could not enrich SmartRecruiters posting {posting_id}: {exc}")
            detailed.append({**item, **detail})
        postings.extend(
            parse_smartrecruiters_payload(detailed, company, company_identifier)
        )
        offset += len(items)
        if offset >= total:
            complete = True
            break
    return SourceResult(
        postings=postings[:limit],
        errors=errors,
        next_cursor="0" if complete else str(offset),
        pages_scanned=max(1, pages),
        complete=complete,
        metadata={"total": total, "company_identifier": company_identifier},
    )


def discover_workday(
    url: str,
    companies: list[str],
    limit: int,
    cursor: str = "",
) -> SourceResult:
    tenant, site, host = workday_site(url)
    if not tenant or not site or not host:
        raise ValueError("Workday URL must contain a tenant and external career site")
    company = configured_company(url, companies)
    public_base = f"https://{host}"
    cxs_base = f"{public_base}/wday/cxs/{tenant}/{site}"
    offset = _offset_cursor(cursor)
    postings: list[JobPosting] = []
    errors: list[str] = []
    pages = 0
    total = 0
    complete = False
    while len(postings) < limit:
        requested = min(20, limit - len(postings))
        payload = post_json(
            f"{cxs_base}/jobs",
            {
                "appliedFacets": {},
                "limit": requested,
                "offset": offset,
                "searchText": "",
            },
        )
        if not isinstance(payload, dict):
            raise ValueError("Workday postings response is not an object")
        pages += 1
        items = (
            payload.get("jobPostings")
            if isinstance(payload.get("jobPostings"), list)
            else []
        )
        total = int(payload.get("total") or len(items))
        if not items:
            complete = True
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            external_path = str(item.get("externalPath") or "")
            detail: object = {}
            if external_path:
                try:
                    detail = fetch_json(f"{cxs_base}{external_path}")
                except Exception as exc:
                    errors.append(
                        f"Could not enrich Workday posting {external_path}: {exc}"
                    )
            posting = parse_workday_posting(
                item,
                detail,
                company,
                public_base,
                site,
            )
            if posting:
                postings.append(posting)
        offset += len(items)
        if offset >= total:
            complete = True
            break
    return SourceResult(
        postings=postings[:limit],
        errors=errors,
        next_cursor="0" if complete else str(offset),
        pages_scanned=max(1, pages),
        complete=complete,
        metadata={"total": total, "tenant": tenant, "site": site},
    )


def discover_source(
    url: str,
    companies: list[str],
    keywords: list[str],
    limit: int,
    cursor: str = "",
) -> SourceResult:
    kind = source_kind(url)
    if kind == "greenhouse":
        return discover_greenhouse(url, companies)
    if kind == "lever":
        return discover_lever(url, companies)
    if kind == "ashby":
        return discover_ashby(url, companies, limit, cursor)
    if kind == "smartrecruiters":
        return discover_smartrecruiters(url, companies, limit, cursor)
    if kind == "workday":
        return discover_workday(url, companies, limit, cursor)
    return discover_generic(url, companies, keywords, limit)
