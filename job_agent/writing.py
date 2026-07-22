from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import GENERATED_DIR
from .db import connect, log, now_iso, row, rows
from .latex import extract_keywords
from .profile import structured_profile


ACTION_PATTERN = re.compile(
    r"\b(?:built|created|designed|developed|implemented|improved|increased|reduced|led|launched|"
    r"managed|optimized|automated|delivered|saved|grew|achieved)\b",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)")
WORD_PATTERN = re.compile(r"[A-Za-z0-9+#.]{3,}")
STOP_WORDS = {
    "and",
    "for",
    "from",
    "that",
    "the",
    "this",
    "using",
    "with",
    "your",
}
CLAIM_STOP_WORDS = STOP_WORDS | {
    "about",
    "also",
    "aligned",
    "application",
    "applying",
    "background",
    "because",
    "candidate",
    "considered",
    "contribute",
    "documented",
    "example",
    "experience",
    "experienced",
    "had",
    "has",
    "have",
    "interest",
    "interested",
    "materials",
    "one",
    "opportunity",
    "profile",
    "record",
    "relevant",
    "role",
    "team",
    "verified",
    "welcome",
    "would",
}
TOKEN_ALIASES = {
    "applied": "use",
    "built": "build",
    "building": "build",
    "engineered": "engineer",
    "engineering": "engineer",
    "reduced": "reduce",
    "reducing": "reduce",
    "used": "use",
    "using": "use",
}
CODEX_TIMEOUT_SECONDS = 600
MAX_LOG_LENGTH = 12_000

_worker_started = False
_worker_lock = threading.Lock()
_worker_event = threading.Event()
_status_lock = threading.Lock()
_status_cached_at = 0.0
_status_cache: dict[str, object] | None = None


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in WORD_PATTERN.findall(value)
        if token.lower() not in STOP_WORDS
    }


def _fact_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in WORD_PATTERN.findall(value):
        token = raw.lower().rstrip(".")
        token = TOKEN_ALIASES.get(token, token)
        if token in CLAIM_STOP_WORDS:
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith(("es", "ss")):
            token = token[:-1]
        tokens.add(token)
    return tokens


def evidence_catalog(limit: int = 140) -> list[dict[str, object]]:
    documents = rows(
        "SELECT id, name, kind, content FROM documents "
        "WHERE ingest_status = 'ready' ORDER BY name, id"
    )
    evidence: list[dict[str, object]] = []
    seen: set[str] = set()
    for document in documents:
        for line_number, raw in enumerate(str(document["content"]).splitlines(), start=1):
            text = _clean(raw.strip(" -*#|\t"))
            key = text.casefold()
            if len(text) < 4 or key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "id": f"D{document['id']}L{line_number}",
                    "text": text[:600],
                    "document_id": document["id"],
                    "source": document["name"],
                    "kind": document["kind"],
                    "line": line_number,
                }
            )
            if len(evidence) >= limit:
                return evidence
    return evidence


def select_evidence(
    job: dict[str, object],
    catalog: list[dict[str, object]] | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    available = catalog if catalog is not None else evidence_catalog()
    terms = extract_keywords(f"{job.get('title', '')} {job.get('description', '')}")
    ranked: list[tuple[int, int, dict[str, object]]] = []
    for index, item in enumerate(available):
        text = str(item["text"])
        lowered = text.lower()
        score = sum(4 for term in terms if term.lower() in lowered)
        score += 3 if ACTION_PATTERN.search(text) else 0
        score += 2 if NUMBER_PATTERN.search(text) else 0
        score += 1 if 25 <= len(text) <= 320 else 0
        ranked.append((score, -index, item))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [item for _, _, item in ranked[:limit]]
    return selected or available[:limit]


def _supporting_evidence_ids(text: str, evidence: list[dict[str, object]], limit: int = 4) -> list[str]:
    target = _tokens(text)
    ranked: list[tuple[int, str]] = []
    for item in evidence:
        overlap = len(target & _tokens(str(item["text"])))
        if overlap:
            ranked.append((overlap, str(item["id"])))
    ranked.sort(reverse=True)
    return [evidence_id for _, evidence_id in ranked[:limit]]


def create_grounded_content(job: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    evidence = select_evidence(job, limit=30)
    profile = structured_profile()
    candidate_name = _clean(profile.get("name")) or "Candidate"
    skills = [str(skill) for skill in profile.get("skills", []) if str(skill).strip()]
    job_blob = f"{job.get('title', '')} {job.get('description', '')}".lower()
    matched_skills = [skill for skill in skills if skill.lower() in job_blob][:8] or skills[:8]

    bullet_evidence = [
        item
        for item in evidence
        if len(str(item["text"])) >= 25
        and "@" not in str(item["text"])
        and not re.fullmatch(r"[+()\d\s.-]+", str(item["text"]))
    ][:5]
    if not bullet_evidence:
        bullet_evidence = evidence[:5]
    bullets = [
        {"text": str(item["text"]), "evidence_ids": [str(item["id"])]}
        for item in bullet_evidence
    ]
    skill_text = ", ".join(matched_skills)
    if skill_text:
        summary = f"Candidate with documented experience in {skill_text}."
    else:
        summary = str(bullet_evidence[0]["text"])
    summary_ids = _supporting_evidence_ids(summary, evidence) or [str(item["id"]) for item in bullet_evidence[:2]]
    strongest = bullets[0] if bullets else {"text": "", "evidence_ids": []}
    strongest_sentence = (
        f"One relevant example from my background is: {strongest['text']}"
        if strongest["text"]
        else ""
    )
    cover = (
        f"Dear {job['company']} Hiring Team,\n\n"
        f"I am interested in the {job['title']} role and its emphasis on the responsibilities described in the posting. "
        f"{strongest_sentence}\n\n"
        "I would welcome the opportunity to discuss how my documented experience can contribute to the team.\n\n"
        f"Best,\n{candidate_name}"
    )
    why_answer = (
        f"I am interested in the {job['title']} role at {job['company']} because its responsibilities align "
        "with the work documented in my application materials."
    )
    claims = [{"text": summary, "evidence_ids": summary_ids}]
    claims.extend({"text": bullet["text"], "evidence_ids": bullet["evidence_ids"]} for bullet in bullets)
    if strongest_sentence:
        claims.append({"text": strongest_sentence, "evidence_ids": strongest["evidence_ids"]})
    content = {
        "resume": {
            "headline": str(job["title"]),
            "summary": summary,
            "bullets": bullets,
        },
        "cover_letter": cover,
        "statements": [
            {"question": "Why are you interested in this role?", "answer": why_answer},
            {
                "question": "Anything else you would like us to know?",
                "answer": "My application materials were tailored from verified source documents and reviewed for unsupported claims.",
            },
        ],
        "email": {
            "subject": f"Interest in {job['title']}",
            "body": (
                f"Hi,\n\nI recently found the {job['title']} opening at {job['company']} and wanted to "
                "briefly introduce myself. I am preparing a tailored application and would appreciate being "
                f"considered for the role.\n\nBest,\n{candidate_name}"
            ),
        },
        "claims": claims,
    }
    return content, evidence


def normalize_content(value: object) -> dict[str, object]:
    content = value if isinstance(value, dict) else {}
    resume = content.get("resume") if isinstance(content.get("resume"), dict) else {}
    bullets = resume.get("bullets") if isinstance(resume.get("bullets"), list) else []
    normalized_bullets = []
    for bullet in bullets:
        if not isinstance(bullet, dict):
            continue
        ids = bullet.get("evidence_ids") if isinstance(bullet.get("evidence_ids"), list) else []
        normalized_bullets.append(
            {"text": _clean(bullet.get("text")), "evidence_ids": [_clean(item) for item in ids if _clean(item)]}
        )
    statements = content.get("statements") if isinstance(content.get("statements"), list) else []
    normalized_statements = [
        {"question": _clean(item.get("question")), "answer": str(item.get("answer") or "").strip()}
        for item in statements
        if isinstance(item, dict)
    ]
    email = content.get("email") if isinstance(content.get("email"), dict) else {}
    claims = content.get("claims") if isinstance(content.get("claims"), list) else []
    normalized_claims = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        ids = claim.get("evidence_ids") if isinstance(claim.get("evidence_ids"), list) else []
        normalized_claims.append(
            {"text": _clean(claim.get("text")), "evidence_ids": [_clean(item) for item in ids if _clean(item)]}
        )
    return {
        "resume": {
            "headline": _clean(resume.get("headline")),
            "summary": _clean(resume.get("summary")),
            "bullets": normalized_bullets,
        },
        "cover_letter": str(content.get("cover_letter") or "").strip(),
        "statements": normalized_statements,
        "email": {
            "subject": _clean(email.get("subject")),
            "body": str(email.get("body") or "").strip(),
        },
        "claims": normalized_claims,
    }


def validate_content(
    value: object,
    evidence: list[dict[str, object]],
    job: dict[str, object],
) -> dict[str, object]:
    content = normalize_content(value)
    evidence_by_id = {str(item["id"]): item for item in evidence}
    errors: list[str] = []
    warnings: list[str] = []
    unsupported: list[str] = []
    resume = content["resume"]
    bullets = resume["bullets"]
    claims = content["claims"]
    if not resume["summary"]:
        errors.append("Resume summary is missing.")
    if not bullets:
        errors.append("At least one resume bullet is required.")
    if not content["cover_letter"]:
        errors.append("Cover letter is missing.")
    if not content["statements"]:
        errors.append("Application statements are missing.")
    if not content["email"]["subject"] or not content["email"]["body"]:
        errors.append("Outreach email is incomplete.")
    if not claims:
        errors.append("Claim-to-evidence mappings are missing.")

    required_claims = [str(resume["summary"])]
    narrative_texts = [
        str(content["cover_letter"]),
        *(str(item["answer"]) for item in content["statements"]),
        str(content["email"]["body"]),
    ]
    for narrative in narrative_texts:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", narrative):
            if ACTION_PATTERN.search(sentence) or NUMBER_PATTERN.search(sentence):
                required_claims.append(sentence)
    referenced: set[str] = set()
    supported_items = list(bullets) + list(claims)
    for item in supported_items:
        text = str(item["text"])
        evidence_ids = list(item["evidence_ids"])
        invalid = [evidence_id for evidence_id in evidence_ids if evidence_id not in evidence_by_id]
        if not evidence_ids:
            unsupported.append(text or "Untitled claim")
            errors.append(f"Unsupported claim: {text[:120] or 'empty claim'}")
            continue
        if invalid:
            errors.append(f"Unknown evidence ID(s) for claim: {', '.join(invalid)}")
            continue
        referenced.update(evidence_ids)
        support_text = " ".join(str(evidence_by_id[evidence_id]["text"]) for evidence_id in evidence_ids)
        numbers = NUMBER_PATTERN.findall(text)
        missing_numbers = [number for number in numbers if number not in support_text]
        if missing_numbers:
            unsupported.append(text)
            errors.append(f"Quantitative claim is not supported by linked evidence: {', '.join(missing_numbers)}")
        unsupported_terms = sorted(_fact_tokens(text) - _fact_tokens(support_text))
        if unsupported_terms:
            unsupported.append(text)
            errors.append(
                "Claim contains term(s) absent from linked evidence: " + ", ".join(unsupported_terms)
            )

    mapped_claim_tokens: set[str] = set()
    for claim in claims:
        evidence_ids = list(claim["evidence_ids"])
        if evidence_ids and all(evidence_id in evidence_by_id for evidence_id in evidence_ids):
            mapped_claim_tokens.update(_fact_tokens(str(claim["text"])))
    for required_claim in required_claims:
        missing_terms = sorted(_fact_tokens(required_claim) - mapped_claim_tokens)
        if missing_terms:
            cleaned_claim = _clean(required_claim)
            errors.append(
                f"Factual text is missing evidence-mapped term(s) ({', '.join(missing_terms)}): "
                f"{cleaned_claim[:120]}"
            )

    complete_text = " ".join(
        [
            str(resume["summary"]),
            *(str(item["text"]) for item in bullets),
            str(content["cover_letter"]),
            *(str(item["answer"]) for item in content["statements"]),
            str(content["email"]["body"]),
        ]
    )
    allowed_number_text = " ".join(
        [
            *(str(item["text"]) for item in evidence),
            str(job.get("title", "")),
            str(job.get("description", "")),
        ]
    )
    unsupported_numbers = sorted(
        {number for number in NUMBER_PATTERN.findall(complete_text) if number not in allowed_number_text}
    )
    if unsupported_numbers:
        errors.append(
            "Draft contains number(s) absent from the evidence and job description: "
            + ", ".join(unsupported_numbers)
        )

    job_keywords = extract_keywords(str(job.get("description", "")))[:12]
    output_text = json.dumps(content, ensure_ascii=True).lower()
    matched_keywords = [keyword for keyword in job_keywords if keyword.lower() in output_text]
    coverage = round((len(matched_keywords) / max(len(job_keywords), 1)) * 100)
    if job_keywords and coverage < 20:
        warnings.append("Job keyword coverage is below 20 percent.")
    status = "failed" if errors else "warning" if warnings else "passed"
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "unsupported_claims": unsupported,
        "evidence_references": len(referenced),
        "evidence_coverage": round((len(referenced) / max(len(evidence), 1)) * 100),
        "keyword_coverage": coverage,
        "matched_keywords": matched_keywords,
        "validated_at": now_iso(),
    }


def save_version(
    application_id: int,
    origin: str,
    content: object,
    evidence: list[dict[str, object]],
    job: dict[str, object],
) -> dict[str, object]:
    normalized = normalize_content(content)
    validation = validate_content(normalized, evidence, job)
    status = "invalid" if validation["status"] == "failed" else "draft"
    with connect() as conn:
        found = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version "
            "FROM writing_versions WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        version_number = int(found["next_version"])
        cursor = conn.execute(
            """
            INSERT INTO writing_versions(
                application_id, version, origin, status, content_json,
                evidence_json, validation_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                application_id,
                version_number,
                origin,
                status,
                json.dumps(normalized),
                json.dumps(evidence),
                json.dumps(validation),
                now_iso(),
            ),
        )
        version_id = int(cursor.lastrowid)
    return get_version(version_id) or {}


def create_initial_version(application_id: int, job: dict[str, object]) -> dict[str, object]:
    content, evidence = create_grounded_content(job)
    return save_version(application_id, "grounded-template", content, evidence, job)


def _parsed_version(version: dict[str, object]) -> dict[str, object]:
    result = dict(version)
    for source, target, fallback in (
        ("content_json", "content", {}),
        ("evidence_json", "evidence", []),
        ("validation_json", "validation", {}),
    ):
        try:
            result[target] = json.loads(str(result.get(source) or json.dumps(fallback)))
        except json.JSONDecodeError:
            result[target] = fallback
        result.pop(source, None)
    return result


def get_version(version_id: int) -> dict[str, object] | None:
    found = row("SELECT * FROM writing_versions WHERE id = ?", (version_id,))
    return _parsed_version(found) if found else None


def application_overview(application_id: int) -> dict[str, object]:
    application = row(
        "SELECT current_writing_version_id, approved_writing_version_id, writing_status, writing_message "
        "FROM applications WHERE id = ?",
        (application_id,),
    )
    if not application:
        return {"current": None, "versions": [], "task": None}
    current_id = application.get("current_writing_version_id")
    current = get_version(int(current_id)) if current_id else None
    versions = rows(
        """
        SELECT id, version, origin, status, validation_json, created_at, approved_at
        FROM writing_versions WHERE application_id = ? ORDER BY version DESC
        """,
        (application_id,),
    )
    for version in versions:
        try:
            version["validation"] = json.loads(str(version.pop("validation_json") or "{}"))
        except json.JSONDecodeError:
            version["validation"] = {}
    task = row(
        "SELECT id, status, message, created_at, started_at, completed_at "
        "FROM writing_tasks WHERE application_id = ? ORDER BY id DESC LIMIT 1",
        (application_id,),
    )
    return {
        "current": current,
        "versions": versions,
        "task": task,
        "status": application["writing_status"],
        "message": application["writing_message"],
        "approved_version_id": application["approved_writing_version_id"],
    }


def _safe_codex_env() -> dict[str, str]:
    allowed = {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "USER",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def codex_status(force: bool = False) -> dict[str, object]:
    global _status_cache, _status_cached_at
    with _status_lock:
        if not force and _status_cache and time.monotonic() - _status_cached_at < 30:
            return dict(_status_cache)
        executable = shutil.which("codex")
        if not executable:
            result = {
                "available": False,
                "ready": False,
                "auth": "missing",
                "message": "Codex CLI is not installed.",
            }
        else:
            try:
                completed = subprocess.run(
                    [executable, "login", "status"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=_safe_codex_env(),
                    check=False,
                )
                output = _clean(f"{completed.stdout} {completed.stderr}")
                if completed.returncode == 0 and "chatgpt" in output.lower():
                    result = {
                        "available": True,
                        "ready": True,
                        "auth": "chatgpt",
                        "message": "Codex CLI is signed in with ChatGPT.",
                    }
                elif completed.returncode == 0:
                    result = {
                        "available": True,
                        "ready": False,
                        "auth": "other",
                        "message": "Codex CLI is not signed in with ChatGPT; writing runs are disabled.",
                    }
                else:
                    result = {
                        "available": True,
                        "ready": False,
                        "auth": "signed-out",
                        "message": "Run codex login and choose ChatGPT authentication.",
                    }
            except (OSError, subprocess.TimeoutExpired) as exc:
                result = {
                    "available": True,
                    "ready": False,
                    "auth": "error",
                    "message": f"Could not check Codex login: {exc}",
                }
        _status_cache = result
        _status_cached_at = time.monotonic()
        return dict(result)


def _output_schema(evidence_ids: list[str]) -> dict[str, object]:
    evidence_array = {
        "type": "array",
        "items": {"type": "string", "enum": evidence_ids},
    }
    supported_text = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "evidence_ids": evidence_array,
        },
        "required": ["text", "evidence_ids"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "resume": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "bullets": {"type": "array", "items": supported_text, "minItems": 1, "maxItems": 6},
                },
                "required": ["headline", "summary", "bullets"],
                "additionalProperties": False,
            },
            "cover_letter": {"type": "string"},
            "statements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "answer": {"type": "string"},
                    },
                    "required": ["question", "answer"],
                    "additionalProperties": False,
                },
                "minItems": 2,
                "maxItems": 2,
            },
            "email": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["subject", "body"],
                "additionalProperties": False,
            },
            "claims": {"type": "array", "items": supported_text, "minItems": 1, "maxItems": 30},
        },
        "required": ["resume", "cover_letter", "statements", "email", "claims"],
        "additionalProperties": False,
    }


def _task_prompt() -> str:
    return (
        "Create concise, job-specific application materials using only input.json in the current directory. "
        "Do not browse, execute project code, or infer facts not present in the evidence array. Every resume "
        "bullet must cite one or more evidence IDs. Add every factual claim from the summary, cover letter, "
        "statements, and email to the claims array with supporting evidence IDs. Preserve numbers exactly; never "
        "invent metrics, employers, dates, degrees, titles, or technologies. Use the candidate name only when it "
        "is present in candidate_profile. Provide exactly two application statements: why this role, and any "
        "additional information. Keep the cover letter under 350 words and the outreach email under 100 words. "
        "Return only the schema-conforming JSON response."
    )


def queue_codex_draft(application_id: int, require_ready: bool = True) -> dict[str, object]:
    application = row(
        """
        SELECT applications.id, applications.status, jobs.title, jobs.company, jobs.description, jobs.location, jobs.url
        FROM applications JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    if not application:
        raise ValueError(f"Application {application_id} does not exist")
    if application["status"] == "submitted":
        raise ValueError("Submitted applications cannot queue new writing versions")
    active = row(
        "SELECT id FROM writing_tasks WHERE application_id = ? AND status IN ('queued', 'running') LIMIT 1",
        (application_id,),
    )
    if active:
        raise ValueError("A Codex writing task is already queued for this application")
    status = codex_status()
    if require_ready and not status["ready"]:
        raise ValueError(str(status["message"]))

    evidence = select_evidence(application, limit=50)
    if not evidence:
        raise ValueError("Ingest at least one source document before requesting a Codex draft")
    request_payload = {
        "job": {
            "title": application["title"],
            "company": application["company"],
            "description": application["description"],
            "location": application["location"],
            "url": application["url"],
        },
        "candidate_profile": structured_profile(),
        "evidence": evidence,
    }
    now = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO writing_tasks(application_id, status, request_json, message, created_at)
            VALUES(?, 'queued', ?, ?, ?)
            """,
            (application_id, json.dumps(request_payload), "Waiting for the local Codex writer.", now),
        )
        task_id = int(cursor.lastrowid)
    task_dir = GENERATED_DIR / "writing" / "tasks" / str(task_id)
    output_path = task_dir / "output.json"
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        git_executable = shutil.which("git")
        if not git_executable:
            raise RuntimeError("Git is required to isolate Codex writing tasks")
        git_init = subprocess.run(
            [git_executable, "init", "--quiet", str(task_dir)],
            capture_output=True,
            text=True,
            timeout=15,
            env=_safe_codex_env(),
            check=False,
        )
        if git_init.returncode != 0:
            raise RuntimeError(f"Could not isolate Codex writing task: {_trim_log(git_init.stderr)}")
        (task_dir / "input.json").write_text(json.dumps(request_payload, indent=2), encoding="utf-8")
        (task_dir / "schema.json").write_text(
            json.dumps(_output_schema([str(item["id"]) for item in evidence]), indent=2),
            encoding="utf-8",
        )
        (task_dir / "prompt.txt").write_text(_task_prompt() + "\n", encoding="utf-8")
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE writing_tasks SET status = 'failed', message = ?, completed_at = ? WHERE id = ?",
                (f"Could not prepare Codex task: {exc}", now_iso(), task_id),
            )
            conn.execute(
                "UPDATE applications SET writing_status = 'failed', writing_message = ?, updated_at = ? WHERE id = ?",
                (f"Could not prepare Codex task: {exc}", now_iso(), application_id),
            )
        raise
    with connect() as conn:
        conn.execute(
            "UPDATE writing_tasks SET task_dir = ?, output_path = ? WHERE id = ?",
            (str(task_dir), str(output_path), task_id),
        )
        conn.execute(
            "UPDATE applications SET writing_status = 'queued', writing_message = ?, updated_at = ? WHERE id = ?",
            ("Codex writing task queued.", now_iso(), application_id),
        )
    log(f"Queued Codex writing task for application {application_id}.", meta={"task_id": task_id})
    _worker_event.set()
    return row("SELECT id, application_id, status, message, created_at FROM writing_tasks WHERE id = ?", (task_id,)) or {}


def _trim_log(value: str) -> str:
    cleaned = value.strip()
    return cleaned if len(cleaned) <= MAX_LOG_LENGTH else cleaned[-MAX_LOG_LENGTH:]


def _claim_next_task() -> dict[str, object] | None:
    with connect() as conn:
        task = conn.execute(
            "SELECT * FROM writing_tasks WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if not task:
            return None
        cursor = conn.execute(
            "UPDATE writing_tasks SET status = 'running', started_at = ?, message = ? "
            "WHERE id = ? AND status = 'queued'",
            (now_iso(), "Codex is generating grounded writing.", int(task["id"])),
        )
        if not cursor.rowcount:
            return None
        conn.execute(
            "UPDATE applications SET writing_status = 'running', writing_message = ?, updated_at = ? WHERE id = ?",
            ("Codex is generating grounded writing.", now_iso(), int(task["application_id"])),
        )
        return dict(task)


def process_next_task(
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, object] | None:
    task = _claim_next_task()
    if not task:
        return None
    task_id = int(task["id"])
    application_id = int(task["application_id"])
    execute = runner or subprocess.run
    task_log = ""
    try:
        if runner is None:
            status = codex_status(force=True)
            if not status["ready"] or status["auth"] != "chatgpt":
                raise RuntimeError(str(status["message"]))
        executable = shutil.which("codex")
        if not executable:
            raise RuntimeError("Codex CLI is not installed")
        task_dir = Path(str(task["task_dir"])).resolve()
        generated_root = (GENERATED_DIR / "writing" / "tasks").resolve()
        if generated_root not in task_dir.parents:
            raise RuntimeError("Writing task directory is outside the generated task root")
        output_path = Path(str(task["output_path"])).resolve()
        if output_path.parent != task_dir:
            raise RuntimeError("Writing output path is outside its task directory")
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--cd",
            str(task_dir),
            "--output-schema",
            str(task_dir / "schema.json"),
            "--output-last-message",
            str(output_path),
            "--color",
            "never",
            _task_prompt(),
        ]
        completed = execute(
            command,
            cwd=task_dir,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT_SECONDS,
            env=_safe_codex_env(),
            check=False,
        )
        task_log = _trim_log(completed.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"Codex exited with code {completed.returncode}: {task_log[-1000:]}")
        content = json.loads(output_path.read_text(encoding="utf-8"))
        request_payload = json.loads(str(task["request_json"]))
        application = row(
            "SELECT applications.*, jobs.title, jobs.company, jobs.description "
            "FROM applications JOIN jobs ON jobs.id = applications.job_id WHERE applications.id = ?",
            (application_id,),
        )
        if not application:
            raise RuntimeError("Application disappeared while the writing task was running")
        version = save_version(
            application_id,
            "codex",
            content,
            list(request_payload["evidence"]),
            application,
        )
        validation = version.get("validation", {})
        if validation.get("status") == "failed":
            with connect() as conn:
                conn.execute(
                    "UPDATE writing_tasks SET result_json = ?, log = ? WHERE id = ?",
                    (json.dumps({"version_id": version["id"], "validation": validation}), task_log, task_id),
                )
            raise RuntimeError("Codex output failed evidence validation: " + "; ".join(validation.get("errors", [])))
        from . import applications

        applications.activate_writing_version(application_id, int(version["id"]))
        message = f"Codex writing version {version['version']} is ready for review."
        with connect() as conn:
            conn.execute(
                """
                UPDATE writing_tasks
                SET status = 'completed', result_json = ?, message = ?, log = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps({"version_id": version["id"]}), message, task_log, now_iso(), task_id),
            )
            conn.execute(
                "UPDATE applications SET writing_status = 'draft', writing_message = ?, updated_at = ? WHERE id = ?",
                (message, now_iso(), application_id),
            )
        log(message, meta={"application_id": application_id, "task_id": task_id})
    except Exception as exc:
        message = f"Codex writing task failed: {exc}"
        failure_log = _trim_log(f"{task_log}\n{exc}")
        with connect() as conn:
            conn.execute(
                "UPDATE writing_tasks SET status = 'failed', message = ?, log = ?, completed_at = ? WHERE id = ?",
                (message, failure_log, now_iso(), task_id),
            )
            conn.execute(
                "UPDATE applications SET writing_status = 'failed', writing_message = ?, updated_at = ? WHERE id = ?",
                (message, now_iso(), application_id),
            )
        log(message, "error", {"application_id": application_id, "task_id": task_id})
    return row("SELECT * FROM writing_tasks WHERE id = ?", (task_id,))


def start_writing_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        with connect() as conn:
            conn.execute(
                "UPDATE writing_tasks SET status = 'queued', message = ? WHERE status = 'running'",
                ("Recovered after a local server restart.",),
            )
        _worker_started = True

        def loop() -> None:
            while True:
                processed = process_next_task()
                if not processed:
                    _worker_event.wait(timeout=3)
                    _worker_event.clear()

        threading.Thread(target=loop, daemon=True, name="applyforme-writing-worker").start()


def save_manual_draft(application_id: int, content: object) -> dict[str, object]:
    application = row(
        "SELECT applications.*, jobs.title, jobs.company, jobs.description "
        "FROM applications JOIN jobs ON jobs.id = applications.job_id WHERE applications.id = ?",
        (application_id,),
    )
    if not application:
        raise ValueError(f"Application {application_id} does not exist")
    if application["status"] == "submitted":
        raise ValueError("Submitted applications cannot create new writing versions")
    current_id = application.get("current_writing_version_id")
    current = get_version(int(current_id)) if current_id else None
    evidence = list(current.get("evidence", [])) if current else select_evidence(application, limit=50)
    version = save_version(application_id, "manual", content, evidence, application)
    if version.get("validation", {}).get("status") == "failed":
        return version
    from . import applications

    applications.activate_writing_version(application_id, int(version["id"]))
    return version


def activate_existing_version(application_id: int, version_id: int) -> dict[str, object]:
    version = get_version(version_id)
    if not version or int(version["application_id"]) != application_id:
        raise ValueError("Writing version does not belong to this application")
    if version.get("validation", {}).get("status") == "failed":
        raise ValueError("An invalid writing version cannot be activated")
    from . import applications

    applications.activate_writing_version(application_id, version_id)
    return application_overview(application_id)


def approve_current_version(application_id: int) -> int:
    application = row(
        "SELECT current_writing_version_id FROM applications WHERE id = ?",
        (application_id,),
    )
    if not application or not application["current_writing_version_id"]:
        raise ValueError("No writing version is active for this application")
    version_id = int(application["current_writing_version_id"])
    version = get_version(version_id)
    if not version or version.get("validation", {}).get("status") == "failed":
        raise ValueError("The current writing version failed evidence validation")
    with connect() as conn:
        conn.execute(
            "UPDATE writing_versions SET status = 'approved', approved_at = ? WHERE id = ?",
            (now_iso(), version_id),
        )
        conn.execute(
            "UPDATE applications SET approved_writing_version_id = ?, writing_status = 'approved', "
            "writing_message = ?, updated_at = ? WHERE id = ?",
            (version_id, f"Writing version {version['version']} approved.", now_iso(), application_id),
        )
    return version_id
