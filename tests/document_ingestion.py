from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
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


def rasterize_as_image_pdf(source: Path, target: Path) -> None:
    prefix = target.parent / ".ocr-raster"
    rendered = subprocess.run(
        [
            "pdftoppm",
            "-f",
            "1",
            "-l",
            "1",
            "-r",
            "200",
            "-singlefile",
            str(source),
            str(prefix),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    ppm = prefix.with_suffix(".ppm")
    raw = ppm.read_bytes()
    position = 0

    def token() -> bytes:
        nonlocal position
        while position < len(raw):
            if raw[position:position + 1] == b"#":
                position = raw.find(b"\n", position) + 1
            elif raw[position:position + 1].isspace():
                position += 1
            else:
                break
        start = position
        while position < len(raw) and not raw[position:position + 1].isspace():
            position += 1
        return raw[start:position]

    assert token() == b"P6"
    width = int(token())
    height = int(token())
    assert token() == b"255"
    while raw[position:position + 1].isspace():
        position += 1
    pixels = raw[position:position + width * height * 3]
    compressed = zlib.compress(pixels)
    content = b"q\n612 0 0 792 0 0 cm\n/Im0 Do\nQ"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"
        ),
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(compressed)} >>\nstream\n"
        ).encode("ascii")
        + compressed
        + b"\nendstream",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, item in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(item)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    target.write_bytes(payload)
    ppm.unlink()


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import documents, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, row, rows, set_setting

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
        ocr_source = Path(tmp) / "ocr-source.pdf"
        write_pdf(
            ocr_source,
            [
                "Scanned Candidate",
                "Python Kubernetes Terraform",
                "Built reliable cloud automation.",
            ],
        )
        rasterize_as_image_pdf(ocr_source, DOCS_DIR / "scanned-resume.pdf")
        (DOCS_DIR / "photo.png").write_bytes(b"not an ingestible document")

        first = profile.ingest_docs()
        assert first == {
            "ingested": 4,
            "pending_review": 0,
            "duplicates": 0,
            "unchanged": 0,
            "skipped": 1,
            "failed": 1,
            "removed": 0,
        }, first

        stored_documents = rows(
            """
            SELECT name, kind, extractor, ingest_status, extraction_confidence
            FROM documents ORDER BY name
            """
        )
        assert len(stored_documents) == 5
        assert next(item for item in stored_documents if item["name"] == "resume.pdf")["extractor"] == "pypdf"
        assert next(item for item in stored_documents if item["name"] == "resume.pdf")["kind"] == "resume"
        assert next(item for item in stored_documents if item["name"] == "transcript.docx")["kind"] == "transcript"
        assert next(item for item in stored_documents if item["name"] == "scanned.pdf")["ingest_status"] == "error"
        ocr_document = next(item for item in stored_documents if item["name"] == "scanned-resume.pdf")
        assert ocr_document["extractor"] == "tesseract-ocr", ocr_document
        assert float(ocr_document["extraction_confidence"]) > 0.4, ocr_document
        assert sum(item["ingest_status"] == "ready" for item in stored_documents) == 4

        structured = profile.structured_profile()
        assert structured["name"] == "Alex Candidate", structured
        assert "alex@example.com" in structured["contact"]["emails"]
        assert "Python" in structured["skills"]
        assert "Playwright" in structured["skills"]
        assert any("University" in item for item in structured["education"])
        assert any("40%" in item for item in structured["highlights"])
        assert len(structured["sources"]) == 4

        second = profile.ingest_docs()
        assert second == {
            "ingested": 0,
            "pending_review": 0,
            "duplicates": 0,
            "unchanged": 4,
            "skipped": 1,
            "failed": 1,
            "removed": 0,
        }, second

        (DOCS_DIR / "portfolio.md").write_text(
            "Playwright, React, and Kubernetes portfolio\nAutomated regression tests for critical workflows.",
            encoding="utf-8",
        )
        updated = profile.ingest_docs()
        assert updated["ingested"] == 1 and updated["unchanged"] == 3 and updated["failed"] == 1, updated
        assert "Kubernetes" in profile.structured_profile()["skills"]

        (DOCS_DIR / "transcript.docx").unlink()
        removed = profile.ingest_docs()
        assert removed["removed"] == 1, removed
        assert row("SELECT id FROM documents WHERE name = 'transcript.docx'") is None
        assert not any("University" in item for item in profile.structured_profile()["education"])

        set_setting("document_review_mode", "true")
        upload = documents.upload_documents(
            [
                {
                    "name": "certification.txt",
                    "content": b"AWS Certified Solutions Architect\nTerraform cloud automation",
                }
            ]
        )
        assert upload["saved"] == 1 and upload["ingestion"]["pending_review"] == 1, upload
        pending = row(
            "SELECT * FROM documents WHERE name = 'certification.txt'"
        )
        assert pending["source"] == "upload"
        assert pending["ingest_status"] == "pending_review"
        assert not any(
            "AWS Certified" in item
            for item in profile.structured_profile()["certifications"]
        )
        approved = documents.approve_document(int(pending["id"]))
        assert approved["ingest_status"] == "ready"
        assert any(
            "AWS Certified" in item
            for item in profile.structured_profile()["certifications"]
        )

        duplicate = documents.upload_documents(
            [
                {
                    "name": "same-certification.txt",
                    "content": b"AWS Certified Solutions Architect\nTerraform cloud automation",
                }
            ]
        )
        assert duplicate["duplicates"] == 1 and duplicate["saved"] == 0, duplicate

        rejected = documents.upload_documents(
            [{"name": "not-really.pdf", "content": b"not a PDF"}]
        )
        assert rejected["rejected"] == 1 and rejected["saved"] == 0, rejected

        updated_document = documents.update_document(
            int(pending["id"]),
            name="cloud-credential.txt",
            kind="certification",
        )
        assert updated_document["name"] == "cloud-credential.txt"
        assert float(updated_document["classification_confidence"]) == 1.0
        archived = documents.archive_document(int(pending["id"]))
        assert archived["ingest_status"] == "archived"
        assert not any(
            "AWS Certified" in item
            for item in profile.structured_profile()["certifications"]
        )
        restored = documents.restore_document(int(pending["id"]))
        assert restored["ingest_status"] == "ready"
        removed_document = documents.remove_document(int(pending["id"]))
        assert removed_document["ok"]
        assert row("SELECT id FROM documents WHERE id = ?", (pending["id"],)) is None

        set_setting("document_review_mode", "false")
        documents.start_watcher()
        for _ in range(30):
            if documents.watcher_status()["last_checked_at"]:
                break
            time.sleep(0.1)
        (DOCS_DIR / "watcher-note.md").write_text(
            "Automatic folder monitoring evidence.",
            encoding="utf-8",
        )
        documents.request_folder_scan()
        watched = None
        for _ in range(40):
            watched = row(
                "SELECT ingest_status FROM documents WHERE name = 'watcher-note.md'"
            )
            if watched:
                break
            time.sleep(0.1)
        assert watched and watched["ingest_status"] == "ready", watched
        assert documents.watcher_status()["running"]

    print("document ingestion ok")


if __name__ == "__main__":
    main()
