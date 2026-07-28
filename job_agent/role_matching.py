from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .job_sources import JobPosting


@dataclass(frozen=True)
class RoleFamily:
    label: str
    aliases: tuple[str, ...]
    anchors: tuple[str, ...]
    competencies: tuple[str, ...]


@dataclass(frozen=True)
class GraduateRoleDecision:
    accepted: bool
    score: int
    reasons: list[str]
    rejection: str = ""
    families: tuple[str, ...] = ()
    required_experience_years: int | None = None


ROLE_FAMILIES: dict[str, RoleFamily] = {
    "product": RoleFamily(
        label="Product management",
        aliases=(
            "associate product manager",
            "junior product manager",
            "graduate product manager",
            "product analyst",
            "product operations analyst",
            "product management analyst",
            "product coordinator",
            "associate product owner",
        ),
        anchors=("product", "product operations", "product owner"),
        competencies=(
            "roadmap",
            "product strategy",
            "user research",
            "customer discovery",
            "backlog",
            "product lifecycle",
            "go to market",
            "stakeholder management",
        ),
    ),
    "project_program": RoleFamily(
        label="Project and program management",
        aliases=(
            "project coordinator",
            "project analyst",
            "assistant project manager",
            "junior project manager",
            "graduate project manager",
            "project management graduate",
            "pmo analyst",
            "pmo coordinator",
            "program coordinator",
            "programme coordinator",
            "delivery analyst",
            "delivery coordinator",
            "implementation analyst",
            "implementation coordinator",
            "portfolio analyst",
        ),
        anchors=(
            "project",
            "program",
            "programme",
            "pmo",
            "delivery",
            "implementation",
            "portfolio",
        ),
        competencies=(
            "project planning",
            "project delivery",
            "program delivery",
            "governance",
            "risk management",
            "resource planning",
            "status reporting",
            "stakeholder management",
        ),
    ),
    "agile_delivery": RoleFamily(
        label="Agile delivery",
        aliases=(
            "agile delivery analyst",
            "agile delivery coordinator",
            "junior scrum master",
            "graduate scrum master",
            "scrum coordinator",
            "agile analyst",
            "business agility analyst",
        ),
        anchors=("agile", "scrum", "business agility"),
        competencies=(
            "scrum",
            "kanban",
            "agile delivery",
            "sprint planning",
            "retrospective",
            "facilitation",
            "continuous improvement",
            "jira",
        ),
    ),
    "consulting": RoleFamily(
        label="Consulting",
        aliases=(
            "graduate consultant",
            "consulting analyst",
            "associate consultant",
            "junior consultant",
            "technology consulting analyst",
            "management consulting analyst",
            "advisory analyst",
            "business analyst",
            "early careers consultant",
            "consulting graduate",
        ),
        anchors=("consulting", "consultant", "advisory", "business analyst"),
        competencies=(
            "client delivery",
            "client engagement",
            "problem solving",
            "business analysis",
            "operating model",
            "process improvement",
            "workshop",
            "stakeholder management",
        ),
    ),
    "change_transformation": RoleFamily(
        label="Change and transformation",
        aliases=(
            "change analyst",
            "change coordinator",
            "organizational change analyst",
            "organisational change analyst",
            "organizational change coordinator",
            "organisational change coordinator",
            "change management analyst",
            "transformation analyst",
            "transformation coordinator",
            "business transformation analyst",
            "adoption analyst",
            "adoption coordinator",
            "communications and change analyst",
        ),
        anchors=(
            "change",
            "transformation",
            "adoption",
            "organizational change",
            "organisational change",
        ),
        competencies=(
            "change management",
            "change impact",
            "stakeholder analysis",
            "communications plan",
            "training",
            "adoption",
            "readiness",
            "organizational change",
            "organisational change",
        ),
    ),
    "strategy_operations": RoleFamily(
        label="Strategy and operations",
        aliases=(
            "strategy analyst",
            "operations analyst",
            "business operations analyst",
            "strategy and operations analyst",
            "management trainee",
            "graduate management trainee",
            "management graduate",
            "commercial graduate",
            "business graduate",
            "rotational program analyst",
            "rotational programme analyst",
        ),
        anchors=(
            "strategy",
            "operations",
            "management trainee",
            "commercial",
            "rotational",
        ),
        competencies=(
            "business strategy",
            "market analysis",
            "operating model",
            "process improvement",
            "business operations",
            "performance management",
            "executive communication",
            "data analysis",
        ),
    ),
}

EARLY_TITLE_SIGNALS = (
    "graduate",
    "new grad",
    "entry level",
    "early career",
    "junior",
    "associate",
    "analyst",
    "coordinator",
    "assistant",
    "trainee",
)
GRADUATE_DESCRIPTION_SIGNALS = (
    "graduate programme",
    "graduate program",
    "graduate scheme",
    "new graduate",
    "new grad",
    "recent graduate",
    "early careers",
    "early career",
    "entry level",
    "entry-level",
    "campus hire",
    "campus recruitment",
    "rotational programme",
    "rotational program",
    "structured training",
    "training provided",
    "mentorship",
    "0-2 years",
    "0 to 2 years",
    "zero to two years",
)
INTERNSHIP_TERMS = ("intern", "internship", "co op", "placement")
DEFAULT_EXCLUDED_TITLE_TERMS = (
    "senior",
    "sr",
    "principal",
    "director",
    "head of",
    "vice president",
    "vp",
    "chief",
    "lead",
)
PREFERRED_SIGNALS = (
    "preferred",
    "ideally",
    "nice to have",
    "nice-to-have",
    "desirable",
    "bonus",
)
NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
NUMBER_TOKEN = r"(?:\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
EXPERIENCE_PATTERNS = (
    re.compile(
        rf"(?P<low>{NUMBER_TOKEN})\s*(?:\+|(?:-|\u2013|\u2014|to)\s*(?P<high>{NUMBER_TOKEN}))?"
        r"\s*(?:years?|yrs?)(?:['\u2019]|\s+of)?(?:\s+[a-z][a-z0-9/&-]*){0,6}\s+experience",
        re.IGNORECASE,
    ),
    re.compile(
        rf"experience(?:\s+[a-z][a-z0-9/&-]*){{0,4}}\s+(?:of\s+)?"
        rf"(?P<low>{NUMBER_TOKEN})\s*(?:\+|(?:-|\u2013|\u2014|to)\s*(?P<high>{NUMBER_TOKEN}))?"
        r"\s*(?:years?|yrs?)",
        re.IGNORECASE,
    ),
)


def split_values(value: object) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[\n,]+", str(value or ""))
        if part.strip()
    ]


def normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9+#.]+", " ", str(value or "").casefold()).strip()


def contains_phrase(blob: str, phrase: str) -> bool:
    normalized_phrase = normalize(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in f" {blob} "


def _number(value: str) -> int:
    normalized = value.casefold()
    return int(normalized) if normalized.isdigit() else NUMBER_WORDS.get(normalized, 0)


def _sentence_context(text: str, start: int, end: int) -> str:
    separators = ".;\n\u2022"
    left = max(text.rfind(mark, 0, start) for mark in separators)
    right_candidates = [
        position
        for mark in separators
        if (position := text.find(mark, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right].strip()


def experience_requirements(description: object) -> dict[str, Any]:
    text = re.sub(r"[^\S\n]+", " ", str(description or "")).strip()
    required: list[int] = []
    preferred: list[int] = []
    evidence: list[str] = []
    occupied: set[tuple[int, int]] = set()
    for pattern in EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span()
            if any(start <= span[0] < end for start, end in occupied):
                continue
            occupied.add(span)
            low = _number(match.group("low"))
            before = text[max(0, span[0] - 24) : span[0]].casefold()
            if re.search(r"\bup to\s*$|\bmaximum(?:\s+of)?\s*$", before):
                low = 0
            context = _sentence_context(text, span[0], span[1])
            context_lower = context.casefold()
            target = preferred if any(term in context_lower for term in PREFERRED_SIGNALS) else required
            target.append(low)
            evidence.append(context[:240])
    return {
        "required_years": max(required) if required else None,
        "preferred_years": max(preferred) if preferred else None,
        "evidence": evidence,
    }


def _selected_families(settings: dict[str, str]) -> list[str]:
    selected = split_values(settings.get("target_role_families", ""))
    return [key for key in selected if key in ROLE_FAMILIES]


def _excluded_title_terms(settings: dict[str, str]) -> list[str]:
    if "excluded_title_terms" not in settings:
        return list(DEFAULT_EXCLUDED_TITLE_TERMS)
    return split_values(settings.get("excluded_title_terms", ""))


def _maximum_required_years(settings: dict[str, str]) -> int:
    try:
        configured = int(
            settings.get("graduate_max_required_experience_years", "2") or "2"
        )
    except (TypeError, ValueError):
        configured = 2
    return max(0, min(20, configured))


def evaluate_graduate_role(
    posting: JobPosting,
    settings: dict[str, str],
) -> GraduateRoleDecision:
    title = normalize(posting.title)
    description = normalize(posting.description)
    include_internships = (
        str(settings.get("graduate_include_internships", "false")).casefold()
        == "true"
    )
    if not include_internships and any(
        contains_phrase(title, term) for term in INTERNSHIP_TERMS
    ):
        return GraduateRoleDecision(False, 0, [], "internships are excluded")

    excluded = next(
        (
            term
            for term in _excluded_title_terms(settings)
            if contains_phrase(title, term)
        ),
        "",
    )
    if excluded:
        return GraduateRoleDecision(
            False,
            0,
            [],
            f"title contains excluded seniority term: {excluded}",
        )

    selected = _selected_families(settings)
    additional_aliases = split_values(settings.get("additional_title_aliases", ""))
    matched_keys: list[str] = []
    exact_alias = False
    for key in selected:
        family = ROLE_FAMILIES[key]
        if any(contains_phrase(title, alias) for alias in family.aliases):
            matched_keys.append(key)
            exact_alias = True

    custom_aliases = [
        alias for alias in additional_aliases if contains_phrase(title, alias)
    ]
    early_signals = [
        signal for signal in EARLY_TITLE_SIGNALS if contains_phrase(title, signal)
    ]
    if not matched_keys and early_signals:
        for key in selected:
            family = ROLE_FAMILIES[key]
            if any(contains_phrase(title, anchor) for anchor in family.anchors):
                matched_keys.append(key)

    if not matched_keys and not custom_aliases:
        return GraduateRoleDecision(
            False,
            0,
            [],
            "title did not match a selected graduate management role family",
        )

    experience = experience_requirements(posting.description)
    max_years = _maximum_required_years(settings)
    required_years = experience["required_years"]
    if required_years is not None and required_years > max_years:
        return GraduateRoleDecision(
            False,
            0,
            [],
            (
                f"job requires at least {required_years} years of experience; "
                f"graduate limit is {max_years}"
            ),
            tuple(matched_keys),
            required_years,
        )

    family_labels = [ROLE_FAMILIES[key].label for key in matched_keys]
    competencies: list[str] = []
    for key in matched_keys:
        for competency in ROLE_FAMILIES[key].competencies:
            if contains_phrase(description, competency) and competency not in competencies:
                competencies.append(competency)
    graduate_signals = [
        signal
        for signal in GRADUATE_DESCRIPTION_SIGNALS
        if contains_phrase(description, signal)
    ]

    score = 55
    score += 10 if exact_alias or custom_aliases else 5
    score += min(10, len(early_signals) * 3)
    score += min(15, len(graduate_signals) * 5)
    score += min(15, len(competencies) * 3)
    score += 5 if required_years is not None else 2
    reasons = []
    if family_labels:
        reasons.append(f"Role family: {', '.join(family_labels[:2])}")
    if custom_aliases:
        reasons.append(f"Custom title: {', '.join(custom_aliases[:2])}")
    if graduate_signals:
        reasons.append(f"Graduate signal: {graduate_signals[0]}")
    if required_years is not None:
        unit = "year" if required_years == 1 else "years"
        reasons.append(f"Required experience: {required_years} {unit}")
    else:
        reasons.append("No experience minimum detected")
    if competencies:
        reasons.append(f"Competencies: {', '.join(competencies[:3])}")
    return GraduateRoleDecision(
        True,
        min(95, score),
        reasons,
        families=tuple(matched_keys),
        required_experience_years=required_years,
    )
