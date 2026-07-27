from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any

from . import profile
from .config import DOCS_DIR, SUPPORTED_DOC_EXTENSIONS
from .db import connect, log, now_iso, row, setting
from .document_readers import MAX_DOCUMENT_BYTES, document_kind, ocr_status


MAX_UPLOAD_FILES = 10
MAX_UPLOAD_REQUEST_BYTES = MAX_DOCUMENT_BYTES * 2
DOCUMENT_KINDS = {
    "resume",
    "transcript",
    "cover_letter",
    "portfolio",
    "work_authorization",
    "certification",
    "source",
}

_watcher_started = False
_watcher_lock = threading.Lock()
_watcher_event = threading.Event()
_watcher_state: dict[str, object] = {
    "running": False,
    "last_checked_at": "",
    "last_changed_at": "",
    "last_result": {},
    "last_error": "",
}


def _safe_name(value: object) -> str:
    candidate = str(value or "").replace("\\", "/").split("/")[-1].strip()
    candidate = re.sub(r"[\x00-\x1f\x7f]", "", candidate)
    candidate = re.sub(r"[^A-Za-z0-9._ ()-]", "_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    if not candidate or candidate.startswith("."):
        raise ValueError("Document filename is invalid")
    if len(candidate) > 160:
        stem = Path(candidate).stem[:120].rstrip()
        candidate = f"{stem}{Path(candidate).suffix}"
    suffix = Path(candidate).suffix.lower()
    if suffix not in SUPPORTED_DOC_EXTENSIONS:
        raise ValueError(f"Unsupported document type: {suffix or 'none'}")
    return candidate


def _validate_payload(name: str, content: bytes) -> None:
    suffix = Path(name).suffix.lower()
    if not content:
        raise ValueError("Document is empty")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"Document exceeds the {MAX_DOCUMENT_BYTES // 1_000_000} MB upload limit"
        )
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise ValueError("The uploaded PDF does not have a valid PDF signature")
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("The uploaded DOCX is missing its document body")
        except zipfile.BadZipFile as exc:
            raise ValueError("The uploaded DOCX is not a valid Office document") from exc
    if suffix in {".txt", ".md", ".tex", ".csv"} and b"\x00" in content[:4096]:
        raise ValueError("The uploaded text document contains binary data")


def _unique_path(directory: Path, name: str) -> Path:
    target = directory / name
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 10_000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate a unique document filename")


def _document_record(document_id: int) -> dict[str, Any]:
    found = row("SELECT * FROM documents WHERE id = ?", (document_id,))
    if not found:
        raise ValueError("Document does not exist")
    try:
        found["metadata"] = json.loads(str(found.get("metadata") or "{}"))
    except json.JSONDecodeError:
        found["metadata"] = {}
    return found


def _managed_path(value: object) -> Path:
    path = Path(str(value or "")).expanduser().resolve()
    docs_root = DOCS_DIR.resolve()
    archive_root = (DOCS_DIR / ".archive").resolve()
    if path.parent not in {docs_root, archive_root}:
        raise ValueError("Document path is outside the managed document folders")
    return path


def upload_documents(files: list[dict[str, object]]) -> dict[str, object]:
    if not files:
        raise ValueError("Choose at least one document to upload")
    if len(files) > MAX_UPLOAD_FILES:
        raise ValueError(f"Upload at most {MAX_UPLOAD_FILES} documents at a time")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    profile.ingest_docs()
    saved_paths: list[Path] = []
    seen_hashes: set[str] = set()
    result: dict[str, object] = {
        "saved": 0,
        "duplicates": 0,
        "rejected": 0,
        "files": [],
        "errors": [],
    }
    for item in files:
        try:
            name = _safe_name(item.get("name"))
            content = item.get("content")
            if not isinstance(content, bytes):
                raise ValueError("Uploaded document content is invalid")
            _validate_payload(name, content)
            digest = hashlib.sha256(content).hexdigest()
            duplicate = row(
                """
                SELECT id, name FROM documents
                WHERE sha256 = ? AND ingest_status IN ('ready', 'pending_review', 'archived')
                ORDER BY id LIMIT 1
                """,
                (digest,),
            )
            if duplicate or digest in seen_hashes:
                result["duplicates"] = int(result["duplicates"]) + 1
                result["files"].append(
                    {
                        "name": name,
                        "status": "duplicate",
                        "duplicate_of_document_id": (
                            int(duplicate["id"]) if duplicate else None
                        ),
                    }
                )
                continue
            target = _unique_path(DOCS_DIR, name)
            temporary = DOCS_DIR / f".{uuid.uuid4().hex}.upload"
            try:
                temporary.write_bytes(content)
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            saved_paths.append(target.resolve())
            seen_hashes.add(digest)
            result["saved"] = int(result["saved"]) + 1
        except (OSError, ValueError, RuntimeError) as exc:
            result["rejected"] = int(result["rejected"]) + 1
            result["errors"].append(
                {"name": str(item.get("name") or "unnamed"), "error": str(exc)}
            )
    ingestion = profile.ingest_docs() if saved_paths else {
        "ingested": 0,
        "pending_review": 0,
        "duplicates": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        "removed": 0,
    }
    if saved_paths:
        placeholders = ",".join("?" for _ in saved_paths)
        with connect() as conn:
            conn.execute(
                f"""
                UPDATE documents SET source = 'upload', updated_at = ?
                WHERE path IN ({placeholders})
                """,
                (now_iso(), *(str(path) for path in saved_paths)),
            )
        uploaded = {
            str(item["path"]): item
            for item in profile.profile_overview()["documents"]
            if str(item["path"]) in {str(path) for path in saved_paths}
        }
        result["files"].extend(
            {
                "id": uploaded.get(str(path), {}).get("id"),
                "name": path.name,
                "status": uploaded.get(str(path), {}).get("ingest_status", "error"),
            }
            for path in saved_paths
        )
        log(
            f"Uploaded {len(saved_paths)} document(s) to the local document inbox.",
            meta={"paths": [str(path) for path in saved_paths]},
        )
        _watcher_event.set()
    result["ingestion"] = ingestion
    return result


def _close_inbox_items(document_id: int, resolution: str) -> None:
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE approval_items
            SET status = 'resolved', resolution = ?, resolved_at = ?, updated_at = ?
            WHERE source_type = 'document' AND source_id = ? AND status = 'pending'
            """,
            (resolution, timestamp, timestamp, document_id),
        )


def approve_document(
    document_id: int,
    *,
    close_inbox: bool = True,
) -> dict[str, object]:
    document = _document_record(document_id)
    if document["ingest_status"] != "pending_review":
        raise ValueError("Only documents awaiting review can be approved")
    with connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET ingest_status = 'ready', review_status = 'approved',
                ingest_error = '', updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), document_id),
        )
    profile.rebuild_structured_profile()
    if close_inbox:
        _close_inbox_items(document_id, "approved_in_documents")
    log(f"Approved document {document['name']} for candidate-profile evidence.")
    return _document_record(document_id)


def retry_document(
    document_id: int,
    *,
    close_inbox: bool = True,
) -> dict[str, object]:
    document = _document_record(document_id)
    if document["ingest_status"] not in {"error", "duplicate"}:
        raise ValueError("Only failed or duplicate documents can be retried")
    profile.ingest_docs()
    result = _document_record(document_id)
    if close_inbox and result["ingest_status"] not in {"error", "duplicate"}:
        _close_inbox_items(document_id, "retried_in_documents")
    return result


def update_document(
    document_id: int,
    *,
    name: object | None = None,
    kind: object | None = None,
) -> dict[str, object]:
    document = _document_record(document_id)
    path = _managed_path(document["path"])
    new_path = path
    if name is not None:
        safe_name = _safe_name(name)
        if Path(safe_name).suffix.lower() != path.suffix.lower():
            raise ValueError("Renaming a document cannot change its file type")
        candidate = path.parent / safe_name
        if candidate != path and candidate.exists():
            raise ValueError("A document with that filename already exists")
        if candidate != path:
            path.replace(candidate)
            new_path = candidate.resolve()
    requested_kind = str(kind or document["kind"]).strip()
    if requested_kind not in DOCUMENT_KINDS:
        raise ValueError("Document classification is invalid")
    confidence = 1.0 if kind is not None else float(
        document.get("classification_confidence") or 0
    )
    metadata = dict(document["metadata"])
    metadata["classification"] = {
        "kind": requested_kind,
        "confidence": confidence,
        "method": "manual" if kind is not None else "filename",
    }
    with connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET path = ?, name = ?, kind = ?, classification_confidence = ?,
                metadata = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(new_path),
                new_path.name,
                requested_kind,
                confidence,
                json.dumps(metadata),
                now_iso(),
                document_id,
            ),
        )
    profile.rebuild_structured_profile()
    log(f"Updated document {document_id}: {new_path.name} ({requested_kind}).")
    _watcher_event.set()
    return _document_record(document_id)


def archive_document(
    document_id: int,
    *,
    close_inbox: bool = True,
) -> dict[str, object]:
    document = _document_record(document_id)
    if document["ingest_status"] == "archived":
        return document
    path = _managed_path(document["path"])
    archive_dir = DOCS_DIR / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_path(archive_dir, path.name)
    path.replace(target)
    metadata = dict(document["metadata"])
    metadata["archived_from_status"] = document["ingest_status"]
    with connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET path = ?, name = ?, ingest_status = 'archived',
                review_status = 'archived', metadata = ?, archived_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                str(target.resolve()),
                target.name,
                json.dumps(metadata),
                now_iso(),
                now_iso(),
                document_id,
            ),
        )
    profile.rebuild_structured_profile()
    if close_inbox:
        _close_inbox_items(document_id, "archived_in_documents")
    log(f"Archived document {document['name']}.", "warning")
    _watcher_event.set()
    return _document_record(document_id)


def restore_document(document_id: int) -> dict[str, object]:
    document = _document_record(document_id)
    if document["ingest_status"] != "archived":
        raise ValueError("Only archived documents can be restored")
    path = _managed_path(document["path"])
    target = _unique_path(DOCS_DIR, path.name)
    path.replace(target)
    metadata = dict(document["metadata"])
    previous = str(metadata.pop("archived_from_status", "pending_review"))
    if previous not in {"ready", "pending_review", "error", "duplicate"}:
        previous = "pending_review"
    review_status = {
        "ready": "approved",
        "pending_review": "pending",
        "error": "error",
        "duplicate": "duplicate",
    }[previous]
    with connect() as conn:
        conn.execute(
            """
            UPDATE documents
            SET path = ?, name = ?, ingest_status = ?, review_status = ?,
                metadata = ?, archived_at = '', updated_at = ?
            WHERE id = ?
            """,
            (
                str(target.resolve()),
                target.name,
                previous,
                review_status,
                json.dumps(metadata),
                now_iso(),
                document_id,
            ),
        )
    profile.ingest_docs()
    log(f"Restored document {target.name}.")
    _watcher_event.set()
    return _document_record(document_id)


def remove_document(
    document_id: int,
    *,
    close_inbox: bool = True,
) -> dict[str, object]:
    document = _document_record(document_id)
    path = _managed_path(document["path"])
    if path.exists():
        path.unlink()
    with connect() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    profile.rebuild_structured_profile()
    if close_inbox:
        _close_inbox_items(document_id, "removed_in_documents")
    log(f"Removed document {document['name']} from local storage.", "warning")
    _watcher_event.set()
    return {"ok": True, "document_id": document_id}


def document_artifact(document_id: int) -> tuple[dict[str, Any], Path]:
    document = _document_record(document_id)
    path = _managed_path(document["path"])
    if not path.is_file():
        raise ValueError("Document file is missing from local storage")
    return document, path


def _folder_signature() -> tuple[tuple[str, int, int], ...]:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    signature: list[tuple[str, int, int]] = []
    for path in sorted(DOCS_DIR.iterdir()):
        if (
            not path.is_file()
            or path.name.startswith(".")
            or path.suffix.lower() not in SUPPORTED_DOC_EXTENSIONS
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def watcher_status() -> dict[str, object]:
    try:
        requested_interval = int(
            setting("document_scan_interval_seconds", "15") or "15"
        )
    except ValueError:
        requested_interval = 15
    interval = max(5, min(requested_interval, 3600))
    return {
        **_watcher_state,
        "interval_seconds": interval,
        "ocr": ocr_status(),
    }


def request_folder_scan() -> None:
    _watcher_event.set()


def start_watcher() -> None:
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return
        _watcher_started = True
        _watcher_state["running"] = True

        def loop() -> None:
            signature: tuple[tuple[str, int, int], ...] | None = None
            while True:
                try:
                    current = _folder_signature()
                    _watcher_state["last_checked_at"] = now_iso()
                    if current != signature:
                        result = profile.ingest_docs()
                        signature = _folder_signature()
                        _watcher_state["last_changed_at"] = now_iso()
                        _watcher_state["last_result"] = result
                        _watcher_state["last_error"] = ""
                except Exception as exc:
                    _watcher_state["last_error"] = str(exc)
                    log(f"Document watcher error: {exc}", "error")
                interval = int(watcher_status()["interval_seconds"])
                _watcher_event.wait(timeout=interval)
                _watcher_event.clear()

        threading.Thread(
            target=loop,
            daemon=True,
            name="applyforme-document-watcher",
        ).start()
