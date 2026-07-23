from __future__ import annotations

import html
import ipaddress
import json
import re
import urllib.robotparser
from collections import Counter, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .db import connect, log, now_iso, row, rows, setting
from .job_sources import USER_AGENT, canonicalize_url, clean_text, fetch_url


EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
NAME_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:[-'][A-Z][a-z]+)?\s+[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?)\b")
LINK_TOKENS = {
    "about",
    "company",
    "contact",
    "leadership",
    "management",
    "our-team",
    "people",
    "team",
    "who-we-are",
}
BLOCK_TAGS = {"address", "article", "div", "h1", "h2", "h3", "h4", "li", "p", "section"}
SKIPPED_TAGS = {"footer", "nav", "noscript", "style", "svg"}
DISALLOWED_HOSTS = {
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
}
ATS_HOST_SUFFIXES = (
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "icims.com",
)
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "outlook.com",
    "proton.me",
    "yahoo.com",
}
IGNORED_LOCAL_PARTS = {
    "abuse",
    "admin",
    "hello",
    "info",
    "legal",
    "noreply",
    "no-reply",
    "press",
    "privacy",
    "sales",
    "security",
    "support",
    "webmaster",
}
GENERIC_ROLE_EMAILS = {
    "careers": ("Recruiting team", 72),
    "hiring": ("Hiring team", 78),
    "jobs": ("Recruiting team", 72),
    "people": ("People team", 66),
    "recruiting": ("Recruiting team", 80),
    "recruitment": ("Recruiting team", 80),
    "talent": ("Talent acquisition", 82),
}
ROLE_PATTERNS = (
    (re.compile(r"\bhiring manager\b", re.I), "Hiring Manager", 100),
    (re.compile(r"\b(?:head|vp|vice president) of engineering\b", re.I), "Engineering Leadership", 96),
    (re.compile(r"\b(?:director|senior director)[, ]+(?:of )?engineering\b", re.I), "Director of Engineering", 94),
    (re.compile(r"\bengineering manager\b", re.I), "Engineering Manager", 92),
    (re.compile(r"\btechnical recruiting manager\b", re.I), "Technical Recruiting Manager", 90),
    (re.compile(r"\btechnical recruiter\b", re.I), "Technical Recruiter", 88),
    (re.compile(r"\btalent acquisition(?: manager| partner| lead)?\b", re.I), "Talent Acquisition", 86),
    (re.compile(r"\brecruit(?:er|ing manager|ing lead)\b", re.I), "Recruiter", 80),
    (re.compile(r"\bpeople (?:operations|partner|lead|team)\b", re.I), "People Team", 68),
    (re.compile(r"\b(?:chief technology officer|cto)\b", re.I), "Chief Technology Officer", 66),
    (re.compile(r"\b(?:founder|co-founder)\b", re.I), "Founder", 60),
)
NAME_EXCLUSIONS = {
    "Chief Technology",
    "Director Engineering",
    "Engineering Leadership",
    "Engineering Manager",
    "Hiring Manager",
    "People Operations",
    "Recruiting Team",
    "Talent Acquisition",
    "Technical Recruiter",
}


@dataclass(frozen=True)
class Candidate:
    name: str
    role: str
    email: str
    email_kind: str
    source_url: str
    confidence: int
    relevance_score: int
    extraction: str


class PublicPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.mailto_contexts: list[tuple[str, str]] = []
        self.lines: list[str] = []
        self.json_ld: list[str] = []
        self._line_parts: list[str] = []
        self._href = ""
        self._link_parts: list[str] = []
        self._json_parts: list[str] = []
        self._capture_json = False
        self._skipped_depth = 0

    def _flush_line(self) -> None:
        value = clean_text(" ".join(self._line_parts))
        if value and (not self.lines or self.lines[-1] != value):
            self.lines.append(value)
        self._line_parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if lower == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._capture_json = True
            self._json_parts = []
            return
        if lower == "script" or lower in SKIPPED_TAGS:
            self._skipped_depth += 1
        if lower in BLOCK_TAGS:
            self._flush_line()
        if lower == "a":
            self._href = attributes.get("href", "")
            self._link_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_json:
            self._json_parts.append(data)
            return
        if self._skipped_depth:
            return
        self._line_parts.append(data)
        if self._href:
            self._link_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower == "script" and self._capture_json:
            self.json_ld.append("".join(self._json_parts))
            self._capture_json = False
            self._json_parts = []
            return
        if lower == "a" and self._href:
            if self._href.casefold().startswith("mailto:"):
                self.mailto_contexts.append((self._href, clean_text(" ".join(self._line_parts))))
            self.links.append((self._href, clean_text(" ".join(self._link_parts))))
            self._href = ""
            self._link_parts = []
        if lower in BLOCK_TAGS:
            self._flush_line()
        if (lower == "script" or lower in SKIPPED_TAGS) and self._skipped_depth:
            self._skipped_depth -= 1

    def finish(self) -> None:
        self._flush_line()


def _json_objects(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        result: list[dict[str, object]] = []
        for item in value:
            result.extend(_json_objects(item))
        return result
    if not isinstance(value, dict):
        return []
    result = [value]
    for item in value.values():
        if isinstance(item, (dict, list)):
            result.extend(_json_objects(item))
    return result


def _is_person(value: dict[str, object]) -> bool:
    kinds = value.get("@type", "")
    candidates = kinds if isinstance(kinds, list) else [kinds]
    return any(str(item).casefold() == "person" for item in candidates)


def _role_details(value: str) -> tuple[str, int]:
    for pattern, label, score in ROLE_PATTERNS:
        if pattern.search(value):
            return label, score
    return "", 0


def _name_from_text(value: str) -> str:
    for match in NAME_PATTERN.finditer(value):
        name = clean_text(match.group(1))
        if name not in NAME_EXCLUSIONS and not _role_details(name)[1]:
            return name
    return ""


def _name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    parts = [part for part in re.split(r"[._-]+", local) if part.isalpha()]
    if len(parts) == 2 and all(2 <= len(part) <= 30 for part in parts):
        return " ".join(part.capitalize() for part in parts)
    return ""


def _email_value(value: object) -> str:
    text = unquote(str(value or "")).strip()
    if text.lower().startswith("mailto:"):
        text = text[7:].split("?", 1)[0]
    found = EMAIL_PATTERN.search(text)
    return found.group(0).lower() if found else ""


def _person_records(parser: PublicPageParser, source_url: str) -> list[tuple[str, str, str, str]]:
    records: list[tuple[str, str, str, str]] = []
    for raw in parser.json_ld:
        try:
            values = _json_objects(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        for item in values:
            if not _is_person(item):
                continue
            records.append(
                (
                    clean_text(item.get("name")),
                    clean_text(item.get("jobTitle")),
                    _email_value(item.get("email")),
                    source_url,
                )
            )
    for index, line in enumerate(parser.lines):
        role, score = _role_details(line)
        if not score:
            continue
        context = " ".join(parser.lines[max(0, index - 1) : index + 1])
        name = _name_from_text(context)
        email = _email_value(line)
        if name or email:
            records.append((name, role, email, source_url))
    return records


def _published_candidates(parser: PublicPageParser, body: str, source_url: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    values = EMAIL_PATTERN.findall(html.unescape(body))
    mailto_context = {
        email: context
        for href, context in parser.mailto_contexts
        if (email := _email_value(href))
    }
    for href, _text in parser.links:
        email = _email_value(href)
        if email:
            values.append(email)
    seen: set[str] = set()
    for value in values:
        email = value.lower()
        if email in seen:
            continue
        seen.add(email)
        local = email.split("@", 1)[0]
        line_index = next(
            (index for index, line in enumerate(parser.lines) if email.casefold() in line.casefold()),
            -1,
        )
        context = mailto_context.get(email, "")
        if line_index >= 0:
            context = " ".join(
                parser.lines[max(0, line_index - 1) : min(len(parser.lines), line_index + 2)]
            )
        role, relevance = _role_details(context)
        if local in GENERIC_ROLE_EMAILS:
            role, relevance = GENERIC_ROLE_EMAILS[local]
        if local in IGNORED_LOCAL_PARTS or relevance < 60:
            continue
        name = _name_from_text(context) or _name_from_email(email)
        confidence = 95 if name and role else 85 if role else 70
        candidates.append(
            Candidate(name, role, email, "published", source_url, confidence, relevance, "public-email")
        )
    return candidates


def _registrable_domain(host: str) -> str:
    value = host.casefold().removeprefix("www.").rstrip(".")
    try:
        ipaddress.ip_address(value)
        return ""
    except ValueError:
        pass
    if value == "localhost" or "." not in value:
        return ""
    parts = value.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _inference_domain(seed_url: str, published: list[Candidate]) -> str:
    domains = [candidate.email.split("@", 1)[1] for candidate in published]
    company_domains = [domain for domain in domains if domain not in PUBLIC_EMAIL_DOMAINS]
    if company_domains:
        return Counter(company_domains).most_common(1)[0][0]
    return _registrable_domain(urlsplit(seed_url).hostname or "")


def _inferred_candidate(name: str, role: str, source_url: str, domain: str) -> Candidate | None:
    normalized_parts = [re.sub(r"[^a-z]", "", part.casefold()) for part in name.split()]
    parts = [part for part in normalized_parts if part]
    _label, relevance = _role_details(role)
    if len(parts) < 2 or relevance < 60 or not domain:
        return None
    email = f"{parts[0]}.{parts[-1]}@{domain}"
    return Candidate(name, role, email, "inferred", source_url, 25, relevance, "name-role-pattern")


def _candidate_links(parser: PublicPageParser, current_url: str, origin: str) -> list[str]:
    origin_parts = urlsplit(origin)
    result: list[str] = []
    for href, text in parser.links:
        absolute = canonicalize_url(urljoin(current_url, href))
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != origin_parts.netloc:
            continue
        blob = f"{parsed.path} {text}".casefold().replace("_", "-")
        if any(token in blob for token in LINK_TOKENS):
            result.append(absolute)
    return result


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _robots(seed_url: str, fetcher: Callable[[str], str]) -> urllib.robotparser.RobotFileParser | None:
    parser = urllib.robotparser.RobotFileParser()
    try:
        parser.set_url(urljoin(_origin(seed_url), "/robots.txt"))
        parser.parse(fetcher(parser.url).splitlines())
        return parser
    except Exception:
        return None


def _seed_url(application: dict[str, object], company_url: str) -> str:
    value = canonicalize_url(company_url or str(application.get("url") or ""))
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter the public company website URL for contact discovery")
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in DISALLOWED_HOSTS):
        raise ValueError("Social-network pages are not supported for contact discovery")
    if not company_url and any(host == suffix or host.endswith(f".{suffix}") for suffix in ATS_HOST_SUFFIXES):
        raise ValueError("This job uses an ATS host; enter the public company website URL")
    return value


def _persist_candidate(candidate: Candidate, company: str, run_id: int) -> tuple[int, bool]:
    existing = row("SELECT * FROM contacts WHERE lower(email) = lower(?)", (candidate.email,))
    now = now_iso()
    verification = "published" if candidate.email_kind == "published" else "unverified"
    metadata = json.dumps(
        {
            "extraction": candidate.extraction,
            "source_url": candidate.source_url,
        }
    )
    if existing:
        with connect() as conn:
            conn.execute(
                """
                UPDATE contacts
                SET name = CASE WHEN name = '' THEN ? ELSE name END,
                    role = CASE WHEN role = '' THEN ? ELSE role END,
                    source_url = CASE WHEN email_kind = 'manual' AND source_url <> '' THEN source_url ELSE ? END,
                    confidence = MAX(confidence, ?), relevance_score = MAX(relevance_score, ?),
                    verification_status = CASE
                      WHEN verification_status IN ('manual', 'verified', 'published', 'rejected') THEN verification_status
                      ELSE ? END,
                    email_kind = CASE
                      WHEN email_kind IN ('manual', 'published') THEN email_kind ELSE ? END,
                    metadata = ?, discovery_run_id = ?, discovered_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    candidate.name,
                    candidate.role,
                    candidate.source_url,
                    candidate.confidence,
                    candidate.relevance_score,
                    verification,
                    candidate.email_kind,
                    metadata,
                    run_id,
                    now,
                    now,
                    int(existing["id"]),
                ),
            )
        return int(existing["id"]), False
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO contacts(
                company, name, role, email, source_url, confidence, verification_status,
                email_kind, relevance_score, metadata, discovery_run_id, notes,
                created_at, updated_at, discovered_at, verified_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, '')
            """,
            (
                company,
                candidate.name,
                candidate.role,
                candidate.email,
                candidate.source_url,
                candidate.confidence,
                verification,
                candidate.email_kind,
                candidate.relevance_score,
                metadata,
                run_id,
                now,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid), True


def _fail_run(run_id: int, company: object, message: str, errors: list[str]) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE contact_discovery_runs
            SET status = 'failed', error = ?, log = ?, completed_at = ? WHERE id = ?
            """,
            (message, json.dumps(errors), now_iso(), run_id),
        )
    log(f"Contact discovery failed for {company}: {message}", "error")


def discover_for_application(
    application_id: int,
    company_url: str = "",
    fetcher: Callable[[str], str] | None = None,
) -> dict[str, object]:
    application = row(
        """
        SELECT applications.id, jobs.title, jobs.company, jobs.url
        FROM applications JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    if not application:
        raise ValueError(f"Application {application_id} does not exist")
    seed = _seed_url(application, company_url.strip())
    try:
        max_pages = min(20, max(1, int(setting("contact_discovery_max_pages", "8") or "8")))
    except ValueError:
        max_pages = 8
    now = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO contact_discovery_runs(application_id, company, seed_url, status, created_at)
            VALUES(?, ?, ?, 'running', ?)
            """,
            (application_id, application["company"], seed, now),
        )
        run_id = int(cursor.lastrowid)

    load = fetcher or fetch_url
    robots = _robots(seed, load)
    origin = _origin(seed)
    pending = deque([seed])
    queued = {seed}
    scanned: list[str] = []
    errors: list[str] = []
    published: list[Candidate] = []
    people: list[tuple[str, str, str, str]] = []
    while pending and len(scanned) < max_pages:
        url = pending.popleft()
        if robots and not robots.can_fetch(USER_AGENT, url):
            errors.append(f"Robots policy disallowed {url}")
            continue
        try:
            body = load(url)
            parser = PublicPageParser()
            parser.feed(body)
            parser.finish()
        except Exception as exc:
            errors.append(f"Could not scan {url}: {exc}")
            continue
        scanned.append(url)
        published.extend(_published_candidates(parser, body, url))
        people.extend(_person_records(parser, url))
        for linked in _candidate_links(parser, url, origin):
            if linked not in queued and len(queued) < max_pages * 3:
                queued.add(linked)
                pending.append(linked)

    if not scanned:
        message = errors[0] if errors else "No public company pages could be scanned"
        _fail_run(run_id, application["company"], message, errors)
        raise ValueError(message)

    domain = _inference_domain(seed, published)
    candidates = list(published)
    for name, role_value, email, source_url in people:
        role, relevance = _role_details(role_value)
        if email:
            if relevance >= 60:
                candidates.append(
                    Candidate(
                        name or _name_from_email(email),
                        role or role_value,
                        email,
                        "published",
                        source_url,
                        95 if name else 85,
                        relevance,
                        "structured-person",
                    )
                )
            continue
        inferred = _inferred_candidate(name, role or role_value, source_url, domain)
        if inferred:
            candidates.append(inferred)

    deduplicated: dict[str, Candidate] = {}
    for candidate in candidates:
        current = deduplicated.get(candidate.email)
        candidate_rank = (
            1 if candidate.email_kind == "published" else 0,
            candidate.relevance_score,
            candidate.confidence,
        )
        current_rank = (
            1 if current and current.email_kind == "published" else 0,
            current.relevance_score if current else 0,
            current.confidence if current else 0,
        )
        if current is None or candidate_rank > current_rank:
            deduplicated[candidate.email] = candidate
    ranked = sorted(
        deduplicated.values(),
        key=lambda item: (-item.relevance_score, -item.confidence, item.email),
    )[:40]
    contact_ids: list[int] = []
    added = 0
    updated = 0
    try:
        for candidate in ranked:
            contact_id, created = _persist_candidate(candidate, str(application["company"]), run_id)
            contact_ids.append(contact_id)
            added += int(created)
            updated += int(not created)
    except Exception as exc:
        message = f"Could not save discovered contacts: {exc}"
        _fail_run(run_id, application["company"], message, errors + [message])
        raise
    status = "complete" if not errors else "complete_with_warnings"
    with connect() as conn:
        conn.execute(
            """
            UPDATE contact_discovery_runs
            SET status = ?, pages_scanned = ?, candidates_found = ?, contacts_added = ?,
                contacts_updated = ?, error = ?, log = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                status,
                len(scanned),
                len(ranked),
                added,
                updated,
                errors[0] if errors else "",
                json.dumps(errors),
                now_iso(),
                run_id,
            ),
        )
    log(
        f"Contact discovery for {application['company']}: {len(scanned)} page(s), "
        f"{added} new contact(s), {updated} refreshed.",
        meta={"run_id": run_id, "application_id": application_id},
    )
    result = row("SELECT * FROM contact_discovery_runs WHERE id = ?", (run_id,)) or {}
    result["contacts"] = [
        found
        for contact_id in contact_ids
        if (found := row("SELECT * FROM contacts WHERE id = ?", (contact_id,)))
    ]
    return result


def verify_contact(contact_id: int) -> dict[str, object]:
    contact = row("SELECT * FROM contacts WHERE id = ?", (contact_id,))
    if not contact:
        raise ValueError(f"Contact {contact_id} does not exist")
    if contact["verification_status"] == "rejected":
        raise ValueError("A rejected contact cannot be verified without a new discovery result")
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "UPDATE contacts SET verification_status = 'verified', verified_at = ?, updated_at = ? WHERE id = ?",
            (now, now, contact_id),
        )
    log(f"Verified outreach contact {contact['email']}.", meta={"contact_id": contact_id})
    return row("SELECT * FROM contacts WHERE id = ?", (contact_id,)) or {}


def reject_contact(contact_id: int) -> dict[str, object]:
    contact = row("SELECT * FROM contacts WHERE id = ?", (contact_id,))
    if not contact:
        raise ValueError(f"Contact {contact_id} does not exist")
    with connect() as conn:
        conn.execute(
            "UPDATE contacts SET verification_status = 'rejected', updated_at = ? WHERE id = ?",
            (now_iso(), contact_id),
        )
    log(f"Rejected discovered contact {contact['email']}.", meta={"contact_id": contact_id})
    return row("SELECT * FROM contacts WHERE id = ?", (contact_id,)) or {}


def list_runs(limit: int = 30) -> list[dict[str, object]]:
    result = rows(
        "SELECT * FROM contact_discovery_runs ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    for item in result:
        try:
            item["log"] = json.loads(str(item.get("log") or "[]"))
        except json.JSONDecodeError:
            item["log"] = []
    return result
