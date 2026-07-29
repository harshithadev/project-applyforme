from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class WorkAuthorization:
    status: str
    label: str
    evidence: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


NEGATIVE_SPONSORSHIP_PATTERNS = (
    re.compile(r"\b(?:no|without)\s+(?:visa\s+)?sponsorship\b", re.IGNORECASE),
    re.compile(r"\b(?:will|do|does|can)(?:\s+not|n't)\s+sponsor\b", re.IGNORECASE),
    re.compile(r"\bunable\s+to\s+(?:provide\s+)?sponsor", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:eligible|available)\s+for\s+(?:visa\s+)?sponsorship\b", re.IGNORECASE),
    re.compile(
        r"\bwithout\s+(?:the\s+)?(?:need\s+for|requiring)\s+sponsorship"
        r"(?:\s+now\s+or\s+in\s+the\s+future)?\b",
        re.IGNORECASE,
    ),
)
OPT_CPT_PATTERNS = (
    re.compile(r"\bCPT\b", re.IGNORECASE),
    re.compile(r"\bOPT\b", re.IGNORECASE),
    re.compile(r"\bcurricular\s+practical\s+training\b", re.IGNORECASE),
    re.compile(r"\boptional\s+practical\s+training\b", re.IGNORECASE),
    re.compile(r"\bF-?1\s+(?:student|visa|status)\b", re.IGNORECASE),
)
POSITIVE_SPONSORSHIP_PATTERNS = (
    re.compile(r"\bvisa\s+sponsorship\s+(?:is\s+)?available\b", re.IGNORECASE),
    re.compile(r"\b(?:will|may|can)\s+(?:provide\s+)?sponsor", re.IGNORECASE),
    re.compile(r"\bsponsorship\s+(?:is\s+)?(?:offered|provided|available)\b", re.IGNORECASE),
    re.compile(r"\bH-?1B\s+(?:visa\s+)?sponsorship\b", re.IGNORECASE),
    re.compile(r"\bimmigration\s+sponsorship\b", re.IGNORECASE),
)

US_SCOPE_ALIASES = {
    "united states",
    "united states of america",
    "u s",
    "u s a",
    "us",
    "usa",
}
US_STATE_NAMES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
    "washington dc",
}
US_STATE_CODES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}


def _normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _contains(blob: str, phrase: str) -> bool:
    return f" {_normalize(phrase)} " in f" {blob} "


def _evidence(text: str, match: re.Match[str]) -> str:
    left = max(text.rfind(mark, 0, match.start()) for mark in ".;\n")
    right_candidates = [
        position
        for mark in ".;\n"
        if (position := text.find(mark, match.end())) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return re.sub(r"\s+", " ", text[left + 1 : right]).strip()[:240]


def classify_work_authorization(value: object) -> WorkAuthorization:
    text = str(value or "")
    for pattern in NEGATIVE_SPONSORSHIP_PATTERNS:
        if match := pattern.search(text):
            return WorkAuthorization(
                "incompatible",
                "No future sponsorship",
                _evidence(text, match),
            )
    for pattern in OPT_CPT_PATTERNS:
        if match := pattern.search(text):
            return WorkAuthorization(
                "cpt_opt",
                "CPT/OPT mentioned",
                _evidence(text, match),
            )
    for pattern in POSITIVE_SPONSORSHIP_PATTERNS:
        if match := pattern.search(text):
            return WorkAuthorization(
                "confirmed",
                "Sponsorship mentioned",
                _evidence(text, match),
            )
    return WorkAuthorization(
        "unknown",
        "Sponsorship needs verification",
    )


def targets_united_states(locations: list[tuple[str, str]]) -> bool:
    return any(term in US_SCOPE_ALIASES for _, term in locations)


def us_location_matches(location: object, workplace_type: object = "") -> bool:
    raw = str(location or "").strip()
    normalized = _normalize(raw)
    workplace = _normalize(workplace_type)
    if any(_contains(normalized, alias) for alias in US_SCOPE_ALIASES):
        return True
    if any(_contains(normalized, state) for state in US_STATE_NAMES):
        return True
    if re.search(
        rf"(?:^|,\s*|\s)\b(?:{'|'.join(sorted(US_STATE_CODES))})\b(?:\s|$)",
        raw,
    ):
        return True
    remote_scope = any(
        _contains(normalized, phrase)
        for phrase in ("worldwide", "anywhere", "global", "north america")
    )
    return workplace == "remote" and (not normalized or remote_scope)
