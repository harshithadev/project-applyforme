from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp
        from job_agent import applications, jobs, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, rows

        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            """
            Alex Candidate
            Software engineer with Python, TypeScript, automation, web scraping, and data workflow experience.
            Built internal tools, dashboards, and browser automation for repetitive operational tasks.
            """,
            encoding="utf-8",
        )
        ingested = profile.ingest_docs()
        assert ingested["ingested"] == 1

        job_id = jobs.add_manual_job(
            {
                "title": "Software Engineer",
                "company": "ExampleCo",
                "url": "https://example.com/jobs/software-engineer",
                "description": "Python TypeScript automation dashboards Playwright",
                "location": "Remote",
            }
        )
        app = applications.draft_application(job_id)
        assert app["id"]
        assert Path(str(app["resume_tex_path"])).exists()
        assert "ExampleCo" in Path(str(app["resume_tex_path"])).read_text(encoding="utf-8")

        rule_id = applications.save_answer_rule("Do you require visa sponsorship?", "No")
        assert rule_id
        applications.approve_application(int(app["id"]))
        result = __import__("job_agent.automation", fromlist=["apply_application"]).apply_application(int(app["id"]))
        assert result["status"] in {"queued", "blocked"}

        assert len(rows("SELECT id FROM documents")) == 1
        assert len(rows("SELECT id FROM jobs")) == 1
        assert len(rows("SELECT id FROM applications")) == 1
    print("smoke ok")


if __name__ == "__main__":
    main()
