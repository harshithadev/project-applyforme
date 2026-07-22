from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree


MAX_DOCUMENT_BYTES = 25_000_000
MAX_EXTRACTED_CHARACTERS = 600_000
MAX_PDF_PAGES = 150
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class DocumentExtraction:
    supported: bool
    content: str = ""
    extractor: str = ""
    error: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


def document_kind(path: Path) -> str:
    name = path.stem.lower()
    patterns = (
        ("resume", ("resume", "curriculum", "cv")),
        ("transcript", ("transcript", "academic", "grades", "coursework")),
        ("cover_letter", ("cover-letter", "cover_letter", "cover letter")),
        ("portfolio", ("portfolio", "project", "work-sample", "work_sample")),
        ("work_authorization", ("visa", "authorization", "immigration")),
        ("certification", ("certificate", "certification", "credential")),
    )
    for kind, tokens in patterns:
        if any(token in name for token in tokens):
            return kind
    return "source"


def read_document(path: Path) -> DocumentExtraction:
    suffix = path.suffix.lower()
    if suffix not in {".txt", ".md", ".tex", ".csv", ".pdf", ".docx"}:
        return DocumentExtraction(supported=False, error=f"Unsupported file type: {suffix or 'none'}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return DocumentExtraction(supported=True, error=f"Could not read file metadata: {exc}")
    if size > MAX_DOCUMENT_BYTES:
        return DocumentExtraction(
            supported=True,
            error=f"Document exceeds the {MAX_DOCUMENT_BYTES // 1_000_000} MB ingestion limit.",
            metadata={"size_bytes": size},
        )
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return DocumentExtraction(supported=True, error=f"Could not read text document: {exc}")
    return DocumentExtraction(
        supported=True,
        content=content[:MAX_EXTRACTED_CHARACTERS],
        extractor="utf-8-text",
        metadata={"size_bytes": size},
    )


def _read_docx(path: Path) -> DocumentExtraction:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                return DocumentExtraction(supported=True, error="DOCX is missing word/document.xml.")
            ordered_parts = sorted(name for name in names if re.fullmatch(r"word/header\d+\.xml", name))
            ordered_parts.append("word/document.xml")
            ordered_parts.extend(sorted(name for name in names if re.fullmatch(r"word/footer\d+\.xml", name)))
            ordered_parts.extend(name for name in ("word/footnotes.xml", "word/endnotes.xml") if name in names)
            sections: list[str] = []
            for name in ordered_parts:
                info = archive.getinfo(name)
                if info.file_size > MAX_DOCUMENT_BYTES:
                    return DocumentExtraction(supported=True, error=f"DOCX part is too large: {name}.")
                part_text = _docx_xml_text(archive.read(name))
                if part_text:
                    sections.append(part_text)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        return DocumentExtraction(supported=True, error=f"Could not parse DOCX: {exc}")
    content = "\n\n".join(sections).strip()[:MAX_EXTRACTED_CHARACTERS]
    if not content:
        return DocumentExtraction(supported=True, error="DOCX contains no readable text.")
    return DocumentExtraction(
        supported=True,
        content=content,
        extractor="docx-ooxml",
        metadata={"parts_read": len(ordered_parts), "size_bytes": path.stat().st_size},
    )


def _docx_xml_text(raw: bytes) -> str:
    root = ElementTree.fromstring(raw)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        chunks: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{WORD_NAMESPACE}t" and node.text:
                chunks.append(node.text)
            elif node.tag == f"{WORD_NAMESPACE}tab":
                chunks.append("\t")
            elif node.tag in {f"{WORD_NAMESPACE}br", f"{WORD_NAMESPACE}cr"}:
                chunks.append("\n")
        text = "".join(chunks).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _read_pdf(path: Path) -> DocumentExtraction:
    try:
        from pypdf import PdfReader
    except ImportError:
        return DocumentExtraction(
            supported=True,
            error="PDF support requires pypdf. Run `npm run setup` and ingest again.",
        )
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                return DocumentExtraction(supported=True, error="PDF is encrypted and requires a password.")
        page_count = len(reader.pages)
        texts: list[str] = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            text = page.extract_text() or ""
            if text.strip():
                texts.append(text.strip())
    except Exception as exc:
        return DocumentExtraction(supported=True, error=f"Could not parse PDF: {exc}")
    content = "\n\n".join(texts).strip()[:MAX_EXTRACTED_CHARACTERS]
    if not content:
        return DocumentExtraction(
            supported=True,
            error="PDF contains no extractable text. A future OCR step is required for scanned pages.",
            metadata={"page_count": page_count, "size_bytes": path.stat().st_size},
        )
    metadata: dict[str, object] = {"page_count": page_count, "size_bytes": path.stat().st_size}
    if page_count > MAX_PDF_PAGES:
        metadata["warning"] = f"Only the first {MAX_PDF_PAGES} pages were read."
    return DocumentExtraction(supported=True, content=content, extractor="pypdf", metadata=metadata)
