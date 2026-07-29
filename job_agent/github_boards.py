from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .job_sources import (
    JobPosting,
    SourceResult,
    canonicalize_url,
    clean_text,
    fetch_url,
)


ROLE_TERMS = (
    "product manager",
    "product management",
    "product operations",
    "project manager",
    "project management",
    "project coordinator",
    "program manager",
    "program management",
    "program coordinator",
    "programme manager",
    "pmo",
    "scrum",
    "agile delivery",
    "agile analyst",
    "consultant",
    "consulting",
    "business analyst",
    "strategy intern",
    "strategy analyst",
    "strategy operations",
    "operations analyst",
    "management trainee",
    "change management",
    "transformation analyst",
    "implementation analyst",
    "delivery analyst",
)
@dataclass
class TableCell:
    text_parts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return clean_text(" ".join(self.text_parts))


class GithubTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[TableCell]] = []
        self._row: list[TableCell] | None = None
        self._cell: TableCell | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lower = tag.casefold()
        if lower == "tr":
            self._row = []
        elif lower in {"td", "th"} and self._row is not None:
            self._cell = TableCell()
        elif lower == "a" and self._cell is not None:
            href = dict(attrs).get("href") or ""
            if href:
                self._cell.links.append(href)
        elif lower in {"br", "hr"} and self._cell is not None:
            self._cell.text_parts.append(" / ")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif lower == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.text_parts.append(data)


def _pipe_table_rows(body: str) -> list[list[TableCell]]:
    rows: list[list[TableCell]] = []
    for line in body.splitlines():
        if not line.startswith("|") or line.count("|") < 5:
            continue
        values = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(values) < 5 or all(re.fullmatch(r"[-: ]+", value) for value in values):
            continue
        cells: list[TableCell] = []
        for value in values[:5]:
            links = re.findall(r'href=["\']([^"\']+)', value, flags=re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", value)
            cells.append(TableCell([text], links))
        rows.append(cells)
    return rows


def _board_rows(body: str) -> list[list[TableCell]]:
    parser = GithubTableParser()
    parser.feed(body)
    return parser.rows + _pipe_table_rows(body)


def _matches_management_role(title: str) -> bool:
    normalized = clean_text(title).casefold()
    return any(term in normalized for term in ROLE_TERMS)


def _board_label(url: str) -> str:
    lowered = url.casefold()
    if "simplifyjobs/summer2026-internships" in lowered:
        return "Simplify Summer Internships"
    if "simplifyjobs/new-grad-positions" in lowered:
        return "Simplify New Grad"
    if "vanshb03/summer2027-internships" in lowered:
        return "Summer 2027 Internships"
    return "Public GitHub job board"


def _attribution_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.hostname != "raw.githubusercontent.com":
        return canonicalize_url(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 4:
        return canonicalize_url(url)
    owner, repo, branch = parts[:3]
    path = "/".join(parts[3:])
    return f"https://github.com/{owner}/{repo}/blob/{branch}/{path}"


def _application_url(cell: TableCell) -> str:
    links = [
        canonicalize_url(link)
        for link in cell.links
        if link.startswith(("https://", "http://"))
    ]
    direct = [
        link
        for link in links
        if "simplify.jobs/p/" not in link.casefold()
        and "simplify.jobs/c/" not in link.casefold()
    ]
    return (direct or links or [""])[0]


def _relative_posted_at(value: str, today: date) -> str:
    normalized = clean_text(value).casefold()
    match = re.fullmatch(r"(\d+)\s*(h|d|w|mo|y)", normalized)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        days = {
            "h": 0,
            "d": amount,
            "w": amount * 7,
            "mo": amount * 30,
            "y": amount * 365,
        }[unit]
        return (today - timedelta(days=days)).isoformat()
    try:
        parsed = datetime.strptime(value.strip(), "%b %d").date().replace(year=today.year)
    except ValueError:
        return ""
    if parsed > today + timedelta(days=7):
        parsed = parsed.replace(year=today.year - 1)
    return parsed.isoformat()


def parse_github_board(
    body: str,
    source_url: str,
    *,
    today: date | None = None,
) -> list[JobPosting]:
    current_company = ""
    current_date = today or datetime.now().astimezone().date()
    postings: list[JobPosting] = []
    seen_urls: set[str] = set()
    for row in _board_rows(body):
        if len(row) < 5:
            continue
        company, title, location, application, age = row[:5]
        raw_title = title.text
        if raw_title.casefold() in {"role", "position"}:
            continue
        raw_company = company.text
        if raw_company and raw_company != "↳":
            current_company = re.sub(r"^[^\w]+", "", raw_company).strip()
        if not current_company or not _matches_management_role(raw_title):
            continue
        if "🔒" in application.text or "🔒" in raw_title:
            continue
        apply_url = _application_url(application)
        if not apply_url or apply_url in seen_urls:
            continue
        seen_urls.add(apply_url)
        restrictions: list[str] = []
        if "🛂" in raw_title:
            restrictions.append("No future sponsorship.")
        if "🇺🇸" in raw_title:
            restrictions.append("Requires U.S. citizenship.")
        if "🎓" in raw_title:
            restrictions.append("Advanced degree indicated.")
        cleaned_title = clean_text(re.sub(r"[🛂🇺🇸🔒🎓🔥]", " ", raw_title))
        external_id = hashlib.sha256(apply_url.encode("utf-8")).hexdigest()[:24]
        postings.append(
            JobPosting(
                title=cleaned_title,
                company=current_company,
                url=apply_url,
                description=clean_text(
                    "Public GitHub job board listing. " + " ".join(restrictions)
                ),
                location=location.text,
                source="github-board",
                posted_at=_relative_posted_at(age.text, current_date),
                external_id=external_id,
                apply_url=apply_url,
                workplace_type=(
                    "remote" if "remote" in location.text.casefold() else ""
                ),
                metadata={
                    "posted_at_precision": "date",
                    "board_age": age.text,
                    "board_url": source_url,
                    "attribution_url": _attribution_url(source_url),
                },
            )
        )
    postings.sort(key=lambda posting: posting.posted_at, reverse=True)
    return postings


def discover_github_board(url: str, limit: int) -> SourceResult:
    postings = parse_github_board(fetch_url(url), url)
    selected_limit = min(200, max(1, limit))
    return SourceResult(
        postings=postings[:selected_limit],
        complete=True,
        metadata={
            "provider": "github-board",
            "provider_label": _board_label(url),
            "total": len(postings),
            "attribution_url": _attribution_url(url),
            "date_precision": "date",
        },
    )
