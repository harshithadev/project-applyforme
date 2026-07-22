from __future__ import annotations

import json
import threading
import time
from mimetypes import guess_type
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import applications, automation, emailer, jobs, profile
from .config import GENERATED_DIR, WEB_DIR
from .db import all_settings, db_info, init_db, log, rows, set_setting
from .latex import available_latex_engine


class Handler(SimpleHTTPRequestHandler):
    server_version = "ApplyForMeLocal/0.1"

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
                    "applications": applications.list_applications(),
                    "answer_rules": rows("SELECT * FROM answer_rules ORDER BY updated_at DESC LIMIT 100"),
                    "events": rows("SELECT * FROM events ORDER BY created_at DESC LIMIT 80"),
                    "paths": db_info(),
                    "latex_engine": available_latex_engine(),
                    "automation": automation.automation_status(),
                }
            )
            return
        if path == "/api/applications/artifact":
            self.send_application_artifact()
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/settings":
                for key, value in payload.items():
                    set_setting(str(key), str(value))
                log("Updated settings from dashboard.")
                self.json({"ok": True})
            elif path == "/api/docs/ingest":
                self.json(profile.ingest_docs())
            elif path == "/api/jobs/scan":
                self.json(jobs.discover_jobs())
            elif path == "/api/jobs":
                self.json({"id": jobs.add_manual_job(payload)})
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
            elif path == "/api/applications/compile":
                self.json(applications.recompile_application(int(payload["application_id"])))
            elif path == "/api/rules":
                self.json({"id": applications.save_answer_rule(str(payload["question"]), str(payload["answer"]))})
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


def main() -> None:
    init_db()
    start_background_scanner()
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
