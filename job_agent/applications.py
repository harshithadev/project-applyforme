from __future__ import annotations

import json
import re
from pathlib import Path

from . import writing
from .db import connect, log, now_iso, row, rows, setting
from .latex import CompilationResult, application_dir, compile_pdf, generate_resume_tex
from .profile import profile_text


RISKY_PATTERNS = re.compile(
    r"salary|compensation|sponsor|visa|authorization|relocat|disabil|gender|race|veteran|criminal|background",
    re.IGNORECASE,
)


def draft_application(job_id: int, mode: str | None = None) -> dict[str, object]:
    job = row("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise ValueError(f"Job {job_id} does not exist")
    if not writing.evidence_catalog(limit=1):
        raise ValueError("Ingest at least one source document before drafting an application")
    active_mode = mode or setting("mode", "review")
    now = now_iso()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO applications(job_id, mode, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (job_id, active_mode, "drafted", now, now),
        )
        application_id = int(cur.lastrowid)
    version = writing.create_initial_version(application_id, job)
    activate_writing_version(application_id, int(version["id"]))
    with connect() as conn:
        conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", ("drafted", now_iso(), job_id))
    log(f"Drafted application package for {job['title']} at {job['company']}.", meta={"application_id": application_id})
    return get_application(application_id) or {}


def activate_writing_version(application_id: int, version_id: int) -> dict[str, object]:
    version = writing.get_version(version_id)
    if not version or int(version["application_id"]) != application_id:
        raise ValueError("Writing version does not belong to this application")
    validation = version.get("validation", {})
    if validation.get("status") == "failed":
        raise ValueError("Writing version failed evidence validation")
    job = row(
        """
        SELECT jobs.*, applications.status AS application_status FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    if not job:
        raise ValueError(f"Application {application_id} does not exist")
    if job["application_status"] == "submitted":
        raise ValueError("Submitted application documents cannot be replaced")
    content = version["content"]
    app_dir = application_dir(application_id)
    tex_path = app_dir / "resume.tex"
    resume_tex = generate_resume_tex(profile_text(), job, tex_path, content.get("resume"))
    compilation = compile_pdf(tex_path)
    email = content.get("email", {})
    with connect() as conn:
        conn.execute(
            """
            UPDATE applications
            SET current_writing_version_id = ?, writing_status = 'draft', writing_message = ?,
                resume_tex_path = ?, resume_pdf_path = ?, resume_compile_status = ?,
                resume_compile_engine = ?, resume_compile_message = ?, resume_compile_log = ?,
                resume_pdf_pages = ?, resume_pdf_bytes = ?, resume_compiled_at = ?,
                cover_letter = ?, statements = ?, email_subject = ?, email_body = ?,
                status = CASE WHEN status = 'submitted' THEN status ELSE 'drafted' END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                version_id,
                f"Writing version {version['version']} is ready for review.",
                resume_tex,
                compilation.pdf_path,
                compilation.status,
                compilation.engine,
                compilation.message,
                compilation.compiler_log,
                compilation.page_count,
                compilation.size_bytes,
                now_iso(),
                content.get("cover_letter", ""),
                json.dumps(content.get("statements", [])),
                email.get("subject", ""),
                email.get("body", ""),
                now_iso(),
                application_id,
            ),
        )
    log(
        f"Activated writing version {version['version']} for application {application_id}.",
        meta={"version_id": version_id, "validation": validation.get("status")},
    )
    return get_application(application_id) or {}


def _save_compilation(application_id: int, compilation: CompilationResult) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE applications
            SET resume_pdf_path = ?, resume_compile_status = ?, resume_compile_engine = ?,
                resume_compile_message = ?, resume_compile_log = ?, resume_pdf_pages = ?,
                resume_pdf_bytes = ?, resume_compiled_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                compilation.pdf_path,
                compilation.status,
                compilation.engine,
                compilation.message,
                compilation.compiler_log,
                compilation.page_count,
                compilation.size_bytes,
                now_iso(),
                now_iso(),
                application_id,
            ),
        )


def recompile_application(application_id: int) -> dict[str, object]:
    app = get_application(application_id)
    if not app:
        raise ValueError(f"Application {application_id} does not exist")
    tex_path = Path(str(app["resume_tex_path"])).resolve()
    expected_dir = application_dir(application_id).resolve()
    if tex_path.parent != expected_dir or tex_path.name != "resume.tex":
        raise ValueError("Application LaTeX path is outside its generated directory")
    compilation = compile_pdf(tex_path)
    _save_compilation(application_id, compilation)
    return get_application(application_id) or {}


def get_application(application_id: int) -> dict[str, object] | None:
    app = row(
        """
        SELECT applications.*, jobs.title, jobs.company, jobs.url, jobs.description
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    if app:
        app["statements"] = json.loads(str(app.get("statements") or "[]"))
        app["writing"] = writing.application_overview(application_id)
    return app


def list_applications() -> list[dict[str, object]]:
    apps = rows(
        """
        SELECT applications.*, jobs.title, jobs.company, jobs.url
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        ORDER BY applications.created_at DESC
        LIMIT 100
        """
    )
    for app in apps:
        app["statements"] = json.loads(str(app.get("statements") or "[]"))
        app["writing"] = writing.application_overview(int(app["id"]))
    return apps


def approve_application(application_id: int) -> None:
    writing.approve_current_version(application_id)
    with connect() as conn:
        conn.execute("UPDATE applications SET status = ?, updated_at = ? WHERE id = ?", ("approved", now_iso(), application_id))
    log(f"Approved application {application_id}.")


def mark_application_submitted(application_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE applications SET status = ?, updated_at = ? WHERE id = ?", ("submitted", now_iso(), application_id))
    log(f"Marked application {application_id} as submitted.")


def save_answer_rule(question: str, answer: str) -> int:
    risky = 1 if RISKY_PATTERNS.search(question) else 0
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO answer_rules(question, answer, category, risky, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(question) DO UPDATE SET
              answer = excluded.answer,
              risky = excluded.risky,
              updated_at = excluded.updated_at
            """,
            (question.strip(), answer.strip(), "application-form", risky, now, now),
        )
        found = conn.execute("SELECT id FROM answer_rules WHERE question = ?", (question.strip(),)).fetchone()
    log(f"Saved answer rule for future forms: {question[:80]}.", meta={"risky": risky})
    return int(found["id"])
