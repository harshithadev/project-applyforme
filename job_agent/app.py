from __future__ import annotations

import json
import threading
import time
from email import policy
from email.parser import BytesParser
from mimetypes import guess_type
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import (
    approvals,
    applications,
    automation,
    contact_discovery,
    documents,
    emailer,
    jobs,
    orchestration,
    outreach,
    profile,
    readiness,
    service,
    writing,
)
from .config import GENERATED_DIR, WEB_DIR
from .db import all_settings, db_info, init_db, log, rows, set_setting
from .latex import available_latex_engine


class Handler(SimpleHTTPRequestHandler):
    server_version = "ApplyForMeLocal/0.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        request_path = parsed.path
        if request_path == "/":
            return str(WEB_DIR / "index.html")
        return str(WEB_DIR / request_path.lstrip("/"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self.json(
                {
                    "settings": all_settings(),
                    "profile": profile.profile_overview(),
                    "jobs": jobs.list_jobs(),
                    "job_source_states": jobs.list_source_states(),
                    "pipeline": {
                        **orchestration.pipeline_status(),
                        "items": orchestration.list_items(),
                    },
                    "applications": applications.list_applications(),
                    "application_tasks": automation.list_tasks(),
                    "answer_rules": rows("SELECT * FROM answer_rules ORDER BY updated_at DESC LIMIT 100"),
                    "contacts": outreach.list_contacts(),
                    "contact_discovery_runs": contact_discovery.list_runs(),
                    "outreach": outreach.list_threads(),
                    "events": rows("SELECT * FROM events ORDER BY created_at DESC LIMIT 80"),
                    "paths": db_info(),
                    "latex_engine": available_latex_engine(),
                    "automation": automation.automation_status(),
                    "codex": writing.codex_status(),
                    "email": outreach.status(),
                    "service": service.status(),
                    "approvals": approvals.inbox_state(),
                    "document_inbox": documents.watcher_status(),
                    "readiness": readiness.readiness_state(),
                }
            )
            return
        if path == "/api/applications/artifact":
            self.send_application_artifact()
            return
        if path == "/api/applications/task-artifact":
            self.send_application_task_artifact()
            return
        if path == "/api/documents/artifact":
            self.send_document_artifact()
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/documents/upload":
                self.json(documents.upload_documents(self.read_multipart_files()))
                return
            payload = self.read_json()
            if path == "/api/settings":
                for key, value in payload.items():
                    set_setting(str(key), str(value))
                log("Updated settings from dashboard.")
                self.json({"ok": True})
            elif path == "/api/docs/ingest":
                self.json(profile.ingest_docs())
            elif path == "/api/documents/approve":
                self.json(documents.approve_document(int(payload["document_id"])))
            elif path == "/api/documents/retry":
                self.json(documents.retry_document(int(payload["document_id"])))
            elif path == "/api/documents/update":
                self.json(
                    documents.update_document(
                        int(payload["document_id"]),
                        name=payload.get("name"),
                        kind=payload.get("kind"),
                    )
                )
            elif path == "/api/documents/archive":
                self.json(documents.archive_document(int(payload["document_id"])))
            elif path == "/api/documents/restore":
                self.json(documents.restore_document(int(payload["document_id"])))
            elif path == "/api/documents/remove":
                self.json(documents.remove_document(int(payload["document_id"])))
            elif path == "/api/jobs/scan":
                self.json(jobs.discover_jobs())
            elif path == "/api/jobs":
                self.json({"id": jobs.add_manual_job(payload)})
            elif path == "/api/pipeline/run":
                self.json(orchestration.process_cycle())
            elif path == "/api/pipeline/retry":
                self.json(orchestration.retry_item(int(payload["pipeline_item_id"])))
            elif path == "/api/pipeline/skip":
                self.json(orchestration.skip_item(int(payload["pipeline_item_id"])))
            elif path == "/api/service/restart":
                self.json({"ok": True, "message": "Background service restart scheduled."})
                threading.Thread(
                    target=restart_background_service,
                    daemon=True,
                    name="applyforme-service-restart",
                ).start()
            elif path == "/api/approvals/action":
                self.json(
                    approvals.resolve_item(
                        int(payload["approval_item_id"]),
                        str(payload["action"]),
                        payload.get("payload", {}),
                        str(payload.get("note", "")),
                    )
                )
            elif path == "/api/notifications/test":
                self.json(approvals.send_test_notification())
            elif path == "/api/readiness/run":
                self.json(readiness.run_preflight())
            elif path == "/api/readiness/complete":
                self.json(readiness.complete_setup())
            elif path == "/api/readiness/test-codex":
                self.json(readiness.test_codex_connection())
            elif path == "/api/readiness/test-email":
                self.json(readiness.test_email_connection())
            elif path == "/api/applications/draft":
                self.json(applications.draft_application(int(payload["job_id"]), payload.get("mode")))
            elif path == "/api/applications/approve":
                applications.approve_application(int(payload["application_id"]))
                self.json({"ok": True})
            elif path == "/api/applications/submit":
                applications.mark_application_submitted(int(payload["application_id"]))
                self.json({"ok": True})
            elif path == "/api/applications/apply":
                self.json(automation.apply_application(int(payload["application_id"])))
            elif path == "/api/applications/task/resolve":
                self.json(
                    automation.resolve_checkpoint(
                        int(payload["task_id"]),
                        payload.get("answers", {}),
                        bool(payload.get("approve_submit", False)),
                        bool(payload.get("save_rules", True)),
                    )
                )
            elif path == "/api/applications/task/cancel":
                self.json(automation.cancel_task(int(payload["task_id"])))
            elif path == "/api/applications/compile":
                self.json(applications.recompile_application(int(payload["application_id"])))
            elif path == "/api/applications/writing/queue":
                self.json(writing.queue_codex_draft(int(payload["application_id"])))
            elif path == "/api/applications/writing/save":
                self.json(
                    writing.save_manual_draft(
                        int(payload["application_id"]),
                        payload.get("content", {}),
                    )
                )
            elif path == "/api/applications/writing/activate":
                self.json(
                    writing.activate_existing_version(
                        int(payload["application_id"]),
                        int(payload["version_id"]),
                    )
                )
            elif path == "/api/rules":
                self.json({"id": applications.save_answer_rule(str(payload["question"]), str(payload["answer"]))})
            elif path == "/api/contacts":
                self.json(outreach.create_contact(payload))
            elif path == "/api/contacts/discover":
                self.json(
                    contact_discovery.discover_for_application(
                        int(payload["application_id"]),
                        str(payload.get("company_url", "")),
                    )
                )
            elif path == "/api/contacts/verify":
                self.json(contact_discovery.verify_contact(int(payload["contact_id"])))
            elif path == "/api/contacts/reject":
                self.json(contact_discovery.reject_contact(int(payload["contact_id"])))
            elif path == "/api/outreach/draft":
                self.json(
                    outreach.create_draft(
                        int(payload["application_id"]),
                        int(payload["contact_id"]),
                    )
                )
            elif path == "/api/outreach/save":
                self.json(
                    outreach.save_draft(
                        int(payload["thread_id"]),
                        payload.get("subject", ""),
                        payload.get("body", ""),
                    )
                )
            elif path == "/api/outreach/approve":
                self.json(outreach.approve(int(payload["thread_id"])))
            elif path == "/api/outreach/queue":
                self.json(outreach.queue(int(payload["thread_id"])))
            elif path == "/api/email/send":
                self.json(emailer.send_email(str(payload["to"]), str(payload["subject"]), str(payload["body"])))
            else:
                self.send_error(404, "Unknown API endpoint")
        except Exception as exc:
            log(f"API error on {path}: {exc}", "error")
            self.json({"ok": False, "error": str(exc)}, status=500)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def read_multipart_files(self) -> list[dict[str, object]]:
        content_type = self.headers.get("Content-Type", "")
        if "\r" in content_type or "\n" in content_type or "multipart/form-data" not in content_type:
            raise ValueError("Document uploads require multipart form data")
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            raise ValueError("Document upload is empty")
        if length > documents.MAX_UPLOAD_REQUEST_BYTES:
            raise ValueError(
                f"Upload request exceeds {documents.MAX_UPLOAD_REQUEST_BYTES // 1_000_000} MB"
            )
        raw = self.rfile.read(length)
        message = BytesParser(policy=policy.default).parsebytes(
            (
                f"Content-Type: {content_type}\r\n"
                "MIME-Version: 1.0\r\n\r\n"
            ).encode("ascii")
            + raw
        )
        if not message.is_multipart():
            raise ValueError("Could not parse multipart document upload")
        files: list[dict[str, object]] = []
        for part in message.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            files.append(
                {
                    "name": filename,
                    "content": part.get_payload(decode=True) or b"",
                }
            )
        return files

    def json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_application_artifact(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        try:
            application_id = int(query.get("application_id", [""])[0])
        except ValueError:
            self.send_error(400, "Invalid application ID")
            return
        kind = query.get("kind", [""])[0]
        if kind not in {"pdf", "tex"}:
            self.send_error(400, "Artifact kind must be pdf or tex")
            return
        app = applications.get_application(application_id)
        if not app:
            self.send_error(404, "Application not found")
            return
        field = "resume_pdf_path" if kind == "pdf" else "resume_tex_path"
        artifact = Path(str(app.get(field) or "")).resolve()
        generated_root = GENERATED_DIR.resolve()
        if generated_root not in artifact.parents or not artifact.is_file():
            self.send_error(404, "Artifact not found")
            return
        if artifact.suffix.lower() != f".{kind}":
            self.send_error(400, "Artifact type mismatch")
            return
        data = artifact.read_bytes()
        content_type = guess_type(artifact.name)[0] or "application/octet-stream"
        disposition = "inline" if kind == "pdf" else "attachment"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'{disposition}; filename="application-{application_id}-resume.{kind}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_application_task_artifact(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        try:
            task_id = int(query.get("task_id", [""])[0])
        except ValueError:
            self.send_error(400, "Invalid browser task ID")
            return
        name = Path(query.get("name", [""])[0]).name
        task = automation.get_task(task_id)
        if not task or name not in task.get("screenshots", []):
            self.send_error(404, "Browser task screenshot not found")
            return
        artifact = (Path(str(task["artifact_dir"])) / name).resolve()
        generated_root = (GENERATED_DIR / "browser" / "tasks").resolve()
        if (
            generated_root not in artifact.parents
            or not artifact.is_file()
            or artifact.suffix.lower() != ".png"
        ):
            self.send_error(404, "Browser task screenshot not found")
            return
        data = artifact.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Disposition", f'inline; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_document_artifact(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        try:
            document_id = int(query.get("document_id", [""])[0])
        except ValueError:
            self.send_error(400, "Invalid document ID")
            return
        try:
            document, artifact = documents.document_artifact(document_id)
        except ValueError:
            self.send_error(404, "Document not found")
            return
        data = artifact.read_bytes()
        content_type = guess_type(artifact.name)[0] or "application/octet-stream"
        inline_types = {
            ".pdf",
            ".txt",
            ".md",
            ".tex",
            ".csv",
        }
        disposition = "inline" if artifact.suffix.lower() in inline_types else "attachment"
        download_name = Path(str(document["name"])).name.replace('"', "'").replace("\r", "").replace("\n", "")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'{disposition}; filename="{download_name}"',
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def restart_background_service() -> None:
    time.sleep(0.5)
    try:
        service.restart()
    except Exception as exc:
        log(f"Background service restart failed: {exc}", "error")


def main() -> None:
    init_db()
    documents.start_watcher()
    start_background_scanner()
    writing.start_writing_worker()
    outreach.start_worker()
    automation.start_worker()
    orchestration.start_worker()
    approvals.start_worker()
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    host = "127.0.0.1"
    port = 8787
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ApplyForMe local dashboard: http://{host}:{port}")
    httpd.serve_forever()


def start_background_scanner() -> None:
    def loop() -> None:
        while True:
            try:
                interval = int(all_settings().get("scan_interval_minutes", "0") or "0")
                if interval > 0:
                    jobs.discover_jobs()
                    time.sleep(max(interval, 5) * 60)
                else:
                    time.sleep(30)
            except Exception as exc:
                log(f"Background scanner error: {exc}", "error")
                time.sleep(60)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    main()
