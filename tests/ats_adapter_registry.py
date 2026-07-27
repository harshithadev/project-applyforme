from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def request_json(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode())


def insert_task(connect: object, generated_dir: Path, index: int) -> dict[str, object]:
    from job_agent.db import now_iso, row

    current = now_iso()
    with connect() as conn:
        job = conn.execute(
            """
            INSERT INTO jobs(
              title, company, url, description, source, discovered_at, updated_at
            )
            VALUES(?, 'Fixture Company', ?, 'Python systems role.', 'ashby', ?, ?)
            """,
            (
                f"Fixture Engineer {index}",
                f"https://jobs.ashbyhq.com/fixture/{index}",
                current,
                current,
            ),
        )
        application = conn.execute(
            """
            INSERT INTO applications(job_id, mode, status, created_at, updated_at)
            VALUES(?, 'review', 'approved', ?, ?)
            """,
            (int(job.lastrowid), current, current),
        )
        task = conn.execute(
            """
            INSERT INTO application_tasks(
              application_id, adapter, target_url, mode, status, created_at, updated_at
            )
            VALUES(?, 'ashby', ?, 'review', 'queued', ?, ?)
            """,
            (
                int(application.lastrowid),
                f"https://jobs.ashbyhq.com/fixture/{index}",
                current,
                current,
            ),
        )
        task_id = int(task.lastrowid)
        artifact_dir = generated_dir / "browser" / "tasks" / str(task_id)
        artifact_dir.mkdir(parents=True)
        conn.execute(
            "UPDATE application_tasks SET artifact_dir = ? WHERE id = ?",
            (str(artifact_dir), task_id),
        )
    found = row("SELECT * FROM application_tasks WHERE id = ?", (task_id,))
    assert found
    return found


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPLYFORME_ROOT"] = tmp

        from job_agent import app, ats_adapters, automation, browser_diagnostics
        from job_agent.config import GENERATED_DIR
        from job_agent.db import connect, init_db, set_setting

        init_db()
        set_setting("browser_adapter_drift_threshold", "2")

        expected = {
            "https://boards.greenhouse.io/example/jobs/1": "greenhouse",
            "https://jobs.lever.co/example/role": "lever",
            "https://jobs.ashbyhq.com/example/role": "ashby",
            "https://jobs.smartrecruiters.com/Example/role": "smartrecruiters",
            "https://example.wd5.myworkdayjobs.com/Careers/job/1": "workday",
        }
        for url, adapter in expected.items():
            assert ats_adapters.detect_adapter(url) == adapter
            assert ats_adapters.source_kind(url) == adapter
            spec = ats_adapters.definition(adapter)
            assert spec and spec.version and spec.capabilities

        compatible = {
            "url": "https://jobs.ashbyhq.com/fixture/1?token=private",
            "title": "alex@example.test password=fixture-secret",
            "form_count": 1,
            "controls": [
                {
                    "tag": "input",
                    "type": "email",
                    "name": "email",
                    "label": "Email alex@example.test",
                    "required": True,
                },
                {
                    "tag": "input",
                    "type": "file",
                    "name": "resume",
                    "label": "Resume",
                    "required": True,
                },
            ],
            "buttons": [{"tag": "button", "label": "Submit application"}],
        }
        healthy_task = insert_task(connect, GENERATED_DIR, 1)
        healthy_bundle = browser_diagnostics.record_outcome(
            healthy_task,
            status="checkpoint",
            checkpoint_kind="final_review",
            message="Ready for alex@example.test with token=private",
            snapshot=compatible,
        )
        assert healthy_bundle["category"] == "final_review"
        healthy_state = ats_adapters.get_host_state("ashby", "jobs.ashbyhq.com")
        assert healthy_state and healthy_state["status"] == "active"

        drift_snapshot = {
            "url": "https://jobs.ashbyhq.com/fixture/drift?session=private",
            "title": "Unexpected shell for alex@example.test",
            "form_count": 0,
            "controls": [],
            "buttons": [{"tag": "button", "label": "Explore jobs"}],
        }
        for index in (2, 3):
            drift_task = insert_task(connect, GENERATED_DIR, index)
            browser_diagnostics.record_outcome(
                drift_task,
                status="checkpoint",
                checkpoint_kind="unsupported_form",
                message="No supported form for alex@example.test password=fixture-secret",
                snapshot=drift_snapshot,
            )

        quarantined = ats_adapters.get_host_state("ashby", "jobs.ashbyhq.com")
        assert quarantined
        assert quarantined["status"] == "quarantined"
        assert quarantined["consecutive_drift"] == 2
        assert "alex@example.test" not in quarantined["last_message"]
        assert "fixture-secret" not in quarantined["last_message"]
        gate = ats_adapters.attempt_gate(
            {
                "adapter": "ashby",
                "target_url": "https://jobs.ashbyhq.com/fixture/next",
            }
        )
        assert gate["allowed"] is False

        replays = ats_adapters.list_replays()
        assert len(replays) == 3
        drift_replay = next(item for item in replays if item["category"] == "unsupported_form")
        replay_record = ats_adapters.replay_artifact(int(drift_replay["id"]))
        assert replay_record
        replay_path = Path(str(replay_record["artifact_path"]))
        replay_text = replay_path.read_text(encoding="utf-8")
        replay_payload = json.loads(replay_text)
        assert ats_adapters.replay_check(replay_payload)["reproduced"] is True
        for private_value in (
            "alex@example.test",
            "fixture-secret",
            "token=private",
            "session=private",
        ):
            assert private_value not in replay_text

        held_task = insert_task(connect, GENERATED_DIR, 4)
        called = False

        def forbidden_runner(
            _task: dict[str, object],
            _application: dict[str, object],
        ) -> dict[str, object]:
            nonlocal called
            called = True
            raise AssertionError("Quarantined adapter launched a runner")

        assert automation.process_next_task(forbidden_runner) is None
        held = automation.get_task(int(held_task["id"]))
        assert held and held["checkpoint_kind"] == "adapter_quarantined"
        assert held["attempt_count"] == 0
        assert called is False

        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            state = request_json(f"{base_url}/api/state")
            registry = state["ats_adapters"]
            assert registry["summary"]["quarantined"] == 1
            with urllib.request.urlopen(
                f"{base_url}/api/ats-adapters/replay?replay_id={drift_replay['id']}",
                timeout=5,
            ) as response:
                downloaded = json.loads(response.read().decode())
            assert downloaded["id"] == drift_replay["id"]
            reactivated = request_json(
                f"{base_url}/api/ats-adapters/reactivate",
                {"adapter": "ashby", "hostname": "jobs.ashbyhq.com"},
            )
            assert reactivated["ok"] is True
            assert int(held_task["id"]) in reactivated["requeued_task_ids"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        active = ats_adapters.get_host_state("ashby", "jobs.ashbyhq.com")
        assert active and active["status"] == "active"
        requeued = automation.get_task(int(held_task["id"]))
        assert requeued and requeued["status"] == "queued"

    print("ats adapter registry ok")


if __name__ == "__main__":
    main()
