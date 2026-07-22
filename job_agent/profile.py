from __future__ import annotations

import hashlib
from pathlib import Path

from .config import DOCS_DIR, SUPPORTED_DOC_EXTENSIONS
from .db import connect, log, now_iso, rows


def _read_text(path: Path) -> str:
    if path.suffix.lower() not in SUPPORTED_DOC_EXTENSIONS:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _summary(content: str) -> str:
    compact = " ".join(content.split())
    if not compact:
        return "No readable text found."
    return compact[:500]


def ingest_docs() -> dict[str, int]:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    skipped = 0
    with connect() as conn:
        for path in sorted(DOCS_DIR.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            content = _read_text(path)
            if not content:
                skipped += 1
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            conn.execute(
                """
                INSERT INTO documents(path, name, kind, content, summary, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  content = excluded.content,
                  summary = excluded.summary,
                  updated_at = excluded.updated_at
                """,
                (str(path), path.name, "source", content, f"{_summary(content)}\n\nsha256:{digest}", now_iso()),
            )
            count += 1
    log(f"Ingested {count} readable document(s) from docs/.", meta={"skipped": skipped})
    return {"ingested": count, "skipped": skipped}


def profile_text() -> str:
    docs = rows("SELECT name, content FROM documents ORDER BY name")
    if not docs:
        return "No source documents have been ingested yet."
    chunks: list[str] = []
    for doc in docs:
        chunks.append(f"### {doc['name']}\n{doc['content'][:6000]}")
    return "\n\n".join(chunks)


def profile_overview() -> dict[str, object]:
    docs = rows("SELECT id, name, path, summary, updated_at FROM documents ORDER BY name")
    return {"documents": docs, "profile_text": profile_text()[:4000]}
