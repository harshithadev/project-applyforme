from __future__ import annotations

import json
import re

from .db import connect, log, now_iso, row, rows, setting
from .latex import application_dir, compile_pdf, extract_keywords, generate_resume_tex
from .profile import profile_text


RISKY_PATTERNS = re.compile(
    r"salary|compensation|sponsor|visa|authorization|relocat|disabil|gender|race|veteran|criminal|background",
    re.IGNORECASE,
)


def generate_cover_letter(job: dict[str, object], profile: str) -> str:
    title = job["title"]
    company = job["company"]
    keywords = ", ".join(extract_keywords(str(job.get("description", "")))[:8])
    evidence = " ".join(profile.split())[:900]
    return (
        f"Dear {company} Hiring Team,\n\n"
        f"I am interested in the {title} role because it aligns with my background and the needs described in the posting"
        f"{f' around {keywords}' if keywords else ''}. Based on my uploaded resume, transcript, and notes, the strongest evidence to highlight is: {evidence}\n\n"
        "I would welcome the opportunity to discuss how my experience can contribute to the team.\n\n"
        "Best,\n"
        "[Your Name]"
    )


def generate_statements(job: dict[str, object]) -> list[dict[str, str]]:
    company = str(job["company"])
    title = str(job["title"])
    return [
        {
            "question": "Why are you interested in this role?",
            "answer": f"I am interested in the {title} role at {company} because it matches the responsibilities and skills emphasized in the job description, and it gives me a clear opportunity to apply my documented experience to meaningful work.",
        },
        {
            "question": "Anything else you would like us to know?",
            "answer": "I generated this draft from my uploaded source documents and will review it for accuracy before submission.",
        },
    ]


def generate_email(job: dict[str, object]) -> tuple[str, str]:
    title = str(job["title"])
    company = str(job["company"])
    subject = f"Interest in {title}"
    body = (
        f"Hi,\n\n"
        f"I recently found the {title} opening at {company} and wanted to briefly introduce myself. "
        "I am preparing a tailored application and would appreciate being considered for the role. "
        "If you are the right person to contact, I would be glad to share a concise resume or any additional details.\n\n"
        "Best,\n"
        "[Your Name]"
    )
    return subject, body


def draft_application(job_id: int, mode: str | None = None) -> dict[str, object]:
    job = row("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise ValueError(f"Job {job_id} does not exist")
    active_mode = mode or setting("mode", "review")
    profile = profile_text()
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
    app_dir = application_dir(application_id)
    tex_path = app_dir / "resume.tex"
    resume_tex = generate_resume_tex(profile, job, tex_path)
    resume_pdf = compile_pdf(tex_path)
    cover = generate_cover_letter(job, profile)
    statements = generate_statements(job)
    email_subject, email_body = generate_email(job)
    with connect() as conn:
        conn.execute(
            """
            UPDATE applications
            SET resume_tex_path = ?, resume_pdf_path = ?, cover_letter = ?, statements = ?,
                email_subject = ?, email_body = ?, updated_at = ?
            WHERE id = ?
            """,
            (resume_tex, resume_pdf, cover, json.dumps(statements), email_subject, email_body, now_iso(), application_id),
        )
        conn.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", ("drafted", now_iso(), job_id))
    log(f"Drafted application package for {job['title']} at {job['company']}.", meta={"application_id": application_id})
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
    return apps


def approve_application(application_id: int) -> None:
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
