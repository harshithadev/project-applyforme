from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


REPO_ROOT = Path(__file__).resolve().parent.parent


def write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)


def write_pdf(path: Path, lines: list[str]) -> None:
    commands = ["BT", "/F1 12 Tf", "72 720 Td", "15 TL"]
    for index, line in enumerate(lines):
        escaped_line = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            commands.append("T*")
        commands.append(f"({escaped_line}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, content in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(content)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    path.write_bytes(payload)


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, row, rows

        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        write_pdf(
            DOCS_DIR / "resume.pdf",
            [
                "Alex Candidate",
                "alex@example.com | 212-555-0123 | https://example.com/alex",
                "Python TypeScript SQL Docker AWS",
                "Built reliable APIs and reduced deployment time by 40%.",
            ],
        )
        write_docx(
            DOCS_DIR / "transcript.docx",
            ["Example State University", "Master of Science in Computer Science", "GPA 3.9"],
        )
        (DOCS_DIR / "portfolio.md").write_text(
            "Playwright and React portfolio\nAutomated regression tests for critical workflows.",
            encoding="utf-8",
        )
        write_pdf(DOCS_DIR / "scanned.pdf", [])
        (DOCS_DIR / "photo.png").write_bytes(b"not an ingestible document")

        first = profile.ingest_docs()
        assert first == {"ingested": 3, "unchanged": 0, "skipped": 1, "failed": 1, "removed": 0}, first

        documents = rows("SELECT name, kind, extractor, ingest_status FROM documents ORDER BY name")
        assert len(documents) == 4
        assert next(item for item in documents if item["name"] == "resume.pdf")["extractor"] == "pypdf"
        assert next(item for item in documents if item["name"] == "resume.pdf")["kind"] == "resume"
        assert next(item for item in documents if item["name"] == "transcript.docx")["kind"] == "transcript"
        assert next(item for item in documents if item["name"] == "scanned.pdf")["ingest_status"] == "error"
        assert sum(item["ingest_status"] == "ready" for item in documents) == 3

        structured = profile.structured_profile()
        assert structured["name"] == "Alex Candidate", structured
        assert "alex@example.com" in structured["contact"]["emails"]
        assert "Python" in structured["skills"]
        assert "Playwright" in structured["skills"]
        assert any("University" in item for item in structured["education"])
        assert any("40%" in item for item in structured["highlights"])
        assert len(structured["sources"]) == 3

        second = profile.ingest_docs()
        assert second == {"ingested": 0, "unchanged": 3, "skipped": 1, "failed": 1, "removed": 0}, second

        (DOCS_DIR / "portfolio.md").write_text(
            "Playwright, React, and Kubernetes portfolio\nAutomated regression tests for critical workflows.",
            encoding="utf-8",
        )
        updated = profile.ingest_docs()
        assert updated["ingested"] == 1 and updated["unchanged"] == 2 and updated["failed"] == 1, updated
        assert "Kubernetes" in profile.structured_profile()["skills"]

        (DOCS_DIR / "transcript.docx").unlink()
        removed = profile.ingest_docs()
        assert removed["removed"] == 1, removed
        assert row("SELECT id FROM documents WHERE name = 'transcript.docx'") is None
        assert not any("University" in item for item in profile.structured_profile()["education"])

    print("document ingestion ok")


if __name__ == "__main__":
    main()
