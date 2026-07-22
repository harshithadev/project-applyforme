from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp
        os.environ["OPENAI_API_KEY"] = "must-not-leak"
        os.environ["SMTP_PASSWORD"] = "must-not-leak"

        from job_agent import applications, jobs, profile, writing
        from job_agent.config import DOCS_DIR, GENERATED_DIR
        from job_agent.db import init_db, row, rows

        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\n"
            "Software engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.\n"
            "Automated release workflows used by five engineering teams.",
            encoding="utf-8",
        )
        assert profile.ingest_docs()["ingested"] == 1
        job_id = jobs.add_manual_job(
            {
                "title": "Platform Engineer",
                "company": "ExampleCo",
                "url": "https://example.test/jobs/platform",
                "description": "Build Python and TypeScript APIs, SQL systems, and cloud automation.",
                "location": "Remote",
            }
        )
        application = applications.draft_application(job_id)
        application_id = int(application["id"])
        overview = application["writing"]
        current = overview["current"]
        assert current["origin"] == "grounded-template"
        assert current["validation"]["status"] in {"passed", "warning"}
        assert current["evidence"] and current["content"]["resume"]["bullets"]
        assert Path(str(application["resume_pdf_path"])).is_file()
        assert "Alex Candidate" in application["cover_letter"]

        unsupported_content = copy.deepcopy(current["content"])
        unsupported_content["resume"]["bullets"][0]["text"] = "Increased platform throughput by 99%."
        unsupported_content["claims"][0] = {
            "text": "Increased platform throughput by 99%.",
            "evidence_ids": unsupported_content["resume"]["bullets"][0]["evidence_ids"],
        }
        invalid = writing.save_manual_draft(application_id, unsupported_content)
        assert invalid["status"] == "invalid"
        assert invalid["validation"]["status"] == "failed"
        assert "99%" in " ".join(invalid["validation"]["errors"])
        unchanged = row("SELECT current_writing_version_id FROM applications WHERE id = ?", (application_id,))
        assert int(unchanged["current_writing_version_id"]) == int(current["id"])

        unsupported_technology = copy.deepcopy(current["content"])
        unsupported_technology["resume"]["summary"] = "Experienced Kubernetes platform engineer."
        unsupported_technology["claims"][0] = {
            "text": "Experienced Kubernetes platform engineer.",
            "evidence_ids": unsupported_technology["claims"][0]["evidence_ids"],
        }
        invalid_technology = writing.save_manual_draft(application_id, unsupported_technology)
        assert invalid_technology["status"] == "invalid"
        assert "kubernetes" in " ".join(invalid_technology["validation"]["errors"]).lower()

        manual_content = copy.deepcopy(current["content"])
        manual_content["cover_letter"] = manual_content["cover_letter"].replace(
            "I would welcome the opportunity",
            "I welcome the opportunity",
        )
        manual = writing.save_manual_draft(application_id, manual_content)
        assert manual["origin"] == "manual"
        activated = applications.get_application(application_id)
        assert int(activated["current_writing_version_id"]) == int(manual["id"])

        rolled_back = writing.activate_existing_version(application_id, int(current["id"]))
        assert int(rolled_back["current"]["id"]) == int(current["id"])
        applications.approve_application(application_id)
        approved = applications.get_application(application_id)
        assert approved["status"] == "approved"
        assert int(approved["approved_writing_version_id"]) == int(current["id"])

        task = writing.queue_codex_draft(application_id, require_ready=False)
        captured: dict[str, object] = {}

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured["env"] = kwargs["env"]
            task_dir = Path(str(kwargs["cwd"]))
            request = json.loads((task_dir / "input.json").read_text(encoding="utf-8"))
            evidence = request["evidence"][0]
            evidence_id = evidence["id"]
            evidence_text = evidence["text"]
            result = {
                "resume": {
                    "headline": "Platform Engineer",
                    "summary": evidence_text,
                    "bullets": [{"text": evidence_text, "evidence_ids": [evidence_id]}],
                },
                "cover_letter": "Dear ExampleCo Hiring Team,\n\nI am interested in the Platform Engineer role.",
                "statements": [
                    {
                        "question": "Why are you interested in this role?",
                        "answer": "The role aligns with the responsibilities described in the posting.",
                    }
                ],
                "email": {
                    "subject": "Interest in Platform Engineer",
                    "body": "Hi, I am applying for the Platform Engineer role at ExampleCo.",
                },
                "claims": [{"text": evidence_text, "evidence_ids": [evidence_id]}],
            }
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(json.dumps(result), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result), stderr="")

        processed = writing.process_next_task(runner=fake_runner)
        assert processed and processed["status"] == "completed", processed
        command = list(captured["command"])
        assert "--ephemeral" in command
        assert "--ignore-user-config" in command and "--ignore-rules" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--output-schema" in command and "--output-last-message" in command
        safe_env = dict(captured["env"])
        assert "OPENAI_API_KEY" not in safe_env and "SMTP_PASSWORD" not in safe_env
        task_record = row("SELECT task_dir, output_path FROM writing_tasks WHERE id = ?", (task["id"],))
        assert Path(str(task_record["task_dir"])).is_relative_to(GENERATED_DIR / "writing" / "tasks")
        assert (Path(str(task_record["task_dir"])) / ".git").is_dir()
        assert Path(str(task_record["output_path"])).is_file()

        final_application = applications.get_application(application_id)
        assert final_application["writing"]["current"]["origin"] == "codex"
        assert final_application["status"] == "drafted", "A new version must require approval again."
        assert len(rows("SELECT id FROM writing_versions WHERE application_id = ?", (application_id,))) == 5

    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("SMTP_PASSWORD", None)
    print("writing workflow ok")


if __name__ == "__main__":
    main()
