from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import DOCS_DIR, SUPPORTED_DOC_EXTENSIONS
from .db import connect, log, now_iso, row, rows
from .document_readers import DocumentExtraction, document_kind, read_document


EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)")
URL_PATTERN = re.compile(r"\b(?:https?://|www\.)[^\s<>()]+", re.IGNORECASE)
EDUCATION_PATTERN = re.compile(
    r"university|college|institute|school|bachelor|master|ph\.?d|doctorate|degree|gpa|coursework",
    re.IGNORECASE,
)
CERTIFICATION_PATTERN = re.compile(r"certif(?:ied|ication|icate)|credential|license", re.IGNORECASE)
ACHIEVEMENT_PATTERN = re.compile(
    r"\b(?:built|created|designed|developed|implemented|improved|increased|reduced|led|launched|managed|"
    r"optimized|automated|delivered|saved|grew|achieved)\b|\b\d+(?:\.\d+)?%\b",
    re.IGNORECASE,
)
SKILLS = (
    "Python",
    "JavaScript",
    "TypeScript",
    "Java",
    "C++",
    "C#",
    "Go",
    "Rust",
    "Ruby",
    "PHP",
    "Swift",
    "Kotlin",
    "SQL",
    "React",
    "Next.js",
    "Node.js",
    "Django",
    "Flask",
    "FastAPI",
    "Spring",
    "AWS",
    "Azure",
    "GCP",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Git",
    "Linux",
    "PostgreSQL",
    "MySQL",
    "MongoDB",
    "Redis",
    "GraphQL",
    "REST",
    "Playwright",
    "Selenium",
    "Machine Learning",
    "Data Analysis",
    "CI/CD",
)


def _summary(content: str) -> str:
    compact = " ".join(content.split())
    return compact[:500] if compact else "No readable text found."


def _unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(value.split()).strip(" -*|\t")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _candidate_name(documents: list[dict[str, Any]]) -> str:
    section_words = {"resume", "curriculum vitae", "education", "experience", "skills", "summary", "profile"}
    ordered = sorted(documents, key=lambda item: 0 if item["kind"] == "resume" else 1)
    for document in ordered:
        for line in str(document["content"]).splitlines()[:8]:
            cleaned = " ".join(line.split()).strip()
            words = cleaned.split()
            if (
                2 <= len(words) <= 5
                and cleaned.casefold() not in section_words
                and not EMAIL_PATTERN.search(cleaned)
                and not PHONE_PATTERN.search(cleaned)
                and not any(char.isdigit() for char in cleaned)
                and all(re.fullmatch(r"[A-Za-z.'-]+", word) for word in words)
            ):
                return cleaned
    return ""


def build_structured_profile(documents: list[dict[str, Any]]) -> dict[str, object]:
    combined = "\n".join(str(document["content"]) for document in documents)
    lines = _unique(combined.splitlines(), 600)
    lower_combined = combined.casefold()
    skills = [skill for skill in SKILLS if re.search(rf"(?<!\w){re.escape(skill.casefold())}(?!\w)", lower_combined)]
    education = _unique([line for line in lines if EDUCATION_PATTERN.search(line)], 20)
    certifications = _unique([line for line in lines if CERTIFICATION_PATTERN.search(line)], 15)
    highlights = _unique(
        [line for line in lines if 25 <= len(line) <= 320 and ACHIEVEMENT_PATTERN.search(line)],
        30,
    )
    return {
        "version": 1,
        "name": _candidate_name(documents),
        "contact": {
            "emails": _unique(EMAIL_PATTERN.findall(combined), 5),
            "phones": _unique(PHONE_PATTERN.findall(combined), 5),
            "links": _unique([match.rstrip(".,;:") for match in URL_PATTERN.findall(combined)], 10),
        },
        "skills": skills,
        "education": education,
        "certifications": certifications,
        "highlights": highlights,
        "sources": [
            {
                "document_id": document["id"],
                "name": document["name"],
                "kind": document["kind"],
            }
            for document in documents
        ],
        "generated_at": now_iso(),
    }


def _rebuild_structured_profile(conn: Any) -> dict[str, object]:
    documents = [dict(item) for item in conn.execute(
        "SELECT id, name, kind, content FROM documents WHERE ingest_status = 'ready' ORDER BY name"
    ).fetchall()]
    structured = build_structured_profile(documents)
    conn.execute(
        """
        INSERT INTO candidate_profiles(id, profile_json, updated_at) VALUES(1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET profile_json = excluded.profile_json, updated_at = excluded.updated_at
        """,
        (json.dumps(structured), now_iso()),
    )
    return structured


def ingest_docs() -> dict[str, int]:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    result = {"ingested": 0, "unchanged": 0, "skipped": 0, "failed": 0, "removed": 0}
    active_paths: set[str] = set()
    with connect() as conn:
        for path in sorted(DOCS_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SUPPORTED_DOC_EXTENSIONS:
                result["skipped"] += 1
                continue
            path_string = str(path.resolve())
            active_paths.add(path_string)
            try:
                file_bytes = path.read_bytes()
                digest = hashlib.sha256(file_bytes).hexdigest()
                size_bytes = len(file_bytes)
            except OSError as exc:
                digest = ""
                size_bytes = 0
                extraction = DocumentExtraction(supported=True, error=f"Could not read document: {exc}")
            else:
                current = conn.execute(
                    "SELECT sha256, ingest_status FROM documents WHERE path = ?",
                    (path_string,),
                ).fetchone()
                if current and current["sha256"] == digest and current["ingest_status"] == "ready":
                    result["unchanged"] += 1
                    continue
                extraction = read_document(path)
            status = "ready" if extraction.content else "error"
            if status == "ready":
                result["ingested"] += 1
            else:
                result["failed"] += 1
            conn.execute(
                """
                INSERT INTO documents(
                  path, name, kind, content, summary, sha256, extractor, ingest_status,
                  ingest_error, size_bytes, metadata, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  name = excluded.name,
                  kind = excluded.kind,
                  content = excluded.content,
                  summary = excluded.summary,
                  sha256 = excluded.sha256,
                  extractor = excluded.extractor,
                  ingest_status = excluded.ingest_status,
                  ingest_error = excluded.ingest_error,
                  size_bytes = excluded.size_bytes,
                  metadata = excluded.metadata,
                  updated_at = excluded.updated_at
                """,
                (
                    path_string,
                    path.name,
                    document_kind(path),
                    extraction.content,
                    _summary(extraction.content),
                    digest,
                    extraction.extractor,
                    status,
                    extraction.error,
                    size_bytes,
                    json.dumps(extraction.metadata),
                    now_iso(),
                ),
            )
        stored = conn.execute("SELECT id, path FROM documents").fetchall()
        for document in stored:
            if document["path"] not in active_paths:
                conn.execute("DELETE FROM documents WHERE id = ?", (document["id"],))
                result["removed"] += 1
        _rebuild_structured_profile(conn)
    log(
        f"Document ingestion complete: {result['ingested']} updated, {result['unchanged']} unchanged, "
        f"{result['failed']} failed.",
        meta=result,
    )
    return result


def profile_text() -> str:
    documents = rows("SELECT name, kind, content FROM documents WHERE ingest_status = 'ready' ORDER BY name")
    if not documents:
        return "No source documents have been ingested yet."
    return "\n\n".join(
        f"### {document['name']} ({document['kind']})\n{str(document['content'])[:12000]}" for document in documents
    )


def structured_profile() -> dict[str, object]:
    found = row("SELECT profile_json FROM candidate_profiles WHERE id = 1")
    if not found:
        return build_structured_profile([])
    try:
        return json.loads(str(found["profile_json"]))
    except json.JSONDecodeError:
        return build_structured_profile([])


def profile_overview() -> dict[str, object]:
    documents = rows(
        """
        SELECT id, name, path, kind, summary, extractor, ingest_status, ingest_error,
               size_bytes, metadata, updated_at
        FROM documents
        ORDER BY name
        """
    )
    for document in documents:
        try:
            document["metadata"] = json.loads(str(document["metadata"] or "{}"))
        except json.JSONDecodeError:
            document["metadata"] = {}
    return {
        "documents": documents,
        "profile_text": profile_text()[:6000],
        "structured": structured_profile(),
    }
