from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import applications, jobs, profile, writing
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db

        status = writing.codex_status(force=True)
        assert status["ready"] and status["auth"] == "chatgpt", status
        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\n"
            "Platform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        assert profile.ingest_docs()["ingested"] == 1
        job_id = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/platform",
                "description": "Build Python APIs, TypeScript tools, SQL systems, and cloud automation.",
                "location": "Remote",
            }
        )
        application = applications.draft_application(job_id)
        task = writing.queue_codex_draft(int(application["id"]))
        result = writing.process_next_task()
        assert result and result["status"] == "completed", result
        updated = applications.get_application(int(application["id"]))
        current = updated["writing"]["current"]
        assert current["origin"] == "codex"
        assert current["validation"]["status"] in {"passed", "warning"}
        assert current["content"]["resume"]["bullets"]
        print(f"codex live ok: task={task['id']} version={current['version']}")


if __name__ == "__main__":
    main()
