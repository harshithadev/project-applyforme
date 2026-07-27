from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from .config import DOCS_DIR, SUPPORTED_DOC_EXTENSIONS
from .db import connect, log, now_iso, row, rows, setting
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

_ingest_lock = threading.RLock()


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
        "version": 2,
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
                "source": document.get("source", "folder"),
                "extraction_confidence": float(document.get("extraction_confidence") or 0),
                "classification_confidence": float(document.get("classification_confidence") or 0),
            }
            for document in documents
        ],
        "generated_at": now_iso(),
    }


def _rebuild_structured_profile(conn: Any) -> dict[str, object]:
    documents = [dict(item) for item in conn.execute(
        """
        SELECT id, name, kind, content, source, extraction_confidence,
               classification_confidence
        FROM documents
        WHERE ingest_status = 'ready'
        ORDER BY name
        """
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


def rebuild_structured_profile() -> dict[str, object]:
    with connect() as conn:
        return _rebuild_structured_profile(conn)


def ingest_docs() -> dict[str, int]:
    with _ingest_lock:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        result = {
            "ingested": 0,
            "pending_review": 0,
            "duplicates": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
            "removed": 0,
        }
        active_paths: set[str] = set()
        review_mode = setting("document_review_mode", "false") == "true"
        with connect() as conn:
            for path in sorted(DOCS_DIR.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in SUPPORTED_DOC_EXTENSIONS:
                    result["skipped"] += 1
                    continue
                path_string = str(path.resolve())
                active_paths.add(path_string)
                current = conn.execute(
                    """
                    SELECT sha256, ingest_status, review_status, source, created_at
                    FROM documents WHERE path = ?
                    """,
                    (path_string,),
                ).fetchone()
                try:
                    file_bytes = path.read_bytes()
                    digest = hashlib.sha256(file_bytes).hexdigest()
                    size_bytes = len(file_bytes)
                except OSError as exc:
                    digest = ""
                    size_bytes = 0
                    extraction = DocumentExtraction(
                        supported=True,
                        error=f"Could not read document: {exc}",
                    )
                else:
                    if (
                        current
                        and current["sha256"] == digest
                        and current["ingest_status"]
                        in {"ready", "pending_review"}
                    ):
                        result["unchanged"] += 1
                        continue
                    duplicate = conn.execute(
                        """
                        SELECT id, name, path FROM documents
                        WHERE sha256 = ? AND path <> ?
                          AND ingest_status IN ('ready', 'pending_review', 'archived')
                        ORDER BY id LIMIT 1
                        """,
                        (digest, path_string),
                    ).fetchone()
                    if duplicate:
                        extraction = DocumentExtraction(
                            supported=True,
                            error=f"Duplicate of {duplicate['name']}.",
                            metadata={
                                "duplicate_of_document_id": int(duplicate["id"]),
                                "duplicate_of_path": duplicate["path"],
                            },
                        )
                    else:
                        extraction = read_document(path)
                kind = document_kind(path)
                classification_confidence = 0.95 if kind != "source" else 0.35
                metadata = dict(extraction.metadata)
                metadata["classification"] = {
                    "kind": kind,
                    "confidence": classification_confidence,
                    "method": "filename",
                }
                duplicate_of = metadata.get("duplicate_of_document_id")
                if duplicate_of:
                    status = "duplicate"
                    review_status = "duplicate"
                    result["duplicates"] += 1
                elif extraction.content:
                    status = "pending_review" if review_mode else "ready"
                    review_status = "pending" if review_mode else "approved"
                    result["pending_review" if review_mode else "ingested"] += 1
                else:
                    status = "error"
                    review_status = "error"
                    result["failed"] += 1
                timestamp = now_iso()
                conn.execute(
                    """
                    INSERT INTO documents(
                      path, name, kind, content, summary, sha256, extractor,
                      ingest_status, ingest_error, size_bytes, metadata, source,
                      review_status, extraction_confidence,
                      classification_confidence, created_at, archived_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
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
                      review_status = excluded.review_status,
                      extraction_confidence = excluded.extraction_confidence,
                      classification_confidence = excluded.classification_confidence,
                      archived_at = '',
                      updated_at = excluded.updated_at
                    """,
                    (
                        path_string,
                        path.name,
                        kind,
                        extraction.content,
                        _summary(extraction.content),
                        digest,
                        extraction.extractor,
                        status,
                        extraction.error,
                        size_bytes,
                        json.dumps(metadata),
                        str(current["source"]) if current else "folder",
                        review_status,
                        float(metadata.get("extraction_confidence") or 0),
                        classification_confidence,
                        str(current["created_at"]) if current and current["created_at"] else timestamp,
                        timestamp,
                    ),
                )
            stored = conn.execute(
                "SELECT id, path FROM documents WHERE ingest_status <> 'archived'"
            ).fetchall()
            for document in stored:
                if document["path"] not in active_paths:
                    conn.execute("DELETE FROM documents WHERE id = ?", (document["id"],))
                    result["removed"] += 1
            _rebuild_structured_profile(conn)
    if any(
        result[key]
        for key in ("ingested", "pending_review", "duplicates", "failed", "removed")
    ):
        log(
            f"Document ingestion complete: {result['ingested']} updated, "
            f"{result['pending_review']} awaiting review, "
            f"{result['duplicates']} duplicate, {result['failed']} failed, "
            f"{result['removed']} removed.",
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
               size_bytes, metadata, source, review_status, extraction_confidence,
               classification_confidence, created_at, archived_at, updated_at,
               substr(content, 1, 4000) AS content_preview
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
