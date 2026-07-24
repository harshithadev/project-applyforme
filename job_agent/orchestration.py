from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import applications, automation, writing
from .db import all_settings, connect, log, now_iso, row, rows


TERMINAL_STATUSES = {"submitted", "skipped", "cancelled"}
PAUSED_STATUSES = {"blocked", "failed"}
ACTIVE_STATUSES = {
    "queued",
    "running",
    "writing",
    "review",
    "approved",
    "ready",
    "applying",
    "checkpoint",
}

_worker_lock = threading.Lock()
_process_lock = threading.Lock()
_worker_event = threading.Event()
_worker_started = False


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(str(value or default))))
    except ValueError:
        return default


def pipeline_policy(settings: dict[str, str] | None = None) -> dict[str, object]:
    values = settings or all_settings()
    return {
        "enabled": _enabled(values.get("pipeline_enabled", "false")),
        "minimum_score": _bounded_int(values.get("pipeline_min_score"), 75, 0, 100),
        "auto_write": _enabled(values.get("pipeline_auto_write", "true")),
        "auto_approve": _enabled(values.get("pipeline_auto_approve", "false")),
        "auto_apply": _enabled(values.get("pipeline_auto_apply", "true")),
        "mode": values.get("mode", "review"),
        "daily_limit": _bounded_int(values.get("daily_application_limit"), 10, 1, 200),
    }


def _policy_from_item(item: dict[str, Any]) -> dict[str, object]:
    try:
        saved = json.loads(str(item.get("policy_json") or "{}"))
    except json.JSONDecodeError:
        saved = {}
    return saved if isinstance(saved, dict) else {}


def _record_event(
    item_id: int,
    status: str,
    stage: str,
    message: str,
    meta: dict[str, object] | None = None,
) -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pipeline_events(
              pipeline_item_id, status, stage, message, meta, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (item_id, status, stage, message, json.dumps(meta or {}), now),
        )
    level = "error" if status == "failed" else "warning" if status in {"blocked", "checkpoint"} else "info"
    log(
        message,
        level,
        {"pipeline_item_id": item_id, "pipeline_status": status, "pipeline_stage": stage, **(meta or {})},
    )


def _transition(
    item_id: int,
    status: str,
    stage: str,
    message: str,
    *,
    application_id: int | None = None,
    error: str = "",
    meta: dict[str, object] | None = None,
) -> bool:
    current = row("SELECT * FROM pipeline_items WHERE id = ?", (item_id,))
    if not current:
        return False
    app_value = application_id if application_id is not None else current["application_id"]
    completed_at = now_iso() if status in TERMINAL_STATUSES else ""
    changed = any(
        (
            current["status"] != status,
            current["stage"] != stage,
            current["message"] != message,
            current["last_error"] != error,
            current["application_id"] != app_value,
        )
    )
    if not changed:
        return False
    with connect() as conn:
        conn.execute(
            """
            UPDATE pipeline_items
            SET application_id = ?, status = ?, stage = ?, message = ?,
                last_error = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                app_value,
                status,
                stage,
                message,
                error[-4000:],
                now_iso(),
                completed_at,
                item_id,
            ),
        )
    _record_event(item_id, status, stage, message, meta)
    return True


def _increment_attempt(item_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE pipeline_items SET attempt_count = attempt_count + 1, updated_at = ? WHERE id = ?",
            (now_iso(), item_id),
        )


def enqueue_eligible_jobs(force: bool = False) -> dict[str, int]:
    policy = pipeline_policy()
    result = {"queued": 0, "eligible": 0, "daily_remaining": 0}
    if not policy["enabled"] and not force:
        return result

    today = datetime.now(timezone.utc).date().isoformat()
    created_today = row(
        "SELECT COUNT(*) AS count FROM pipeline_items WHERE substr(created_at, 1, 10) = ?",
        (today,),
    )
    remaining = max(0, int(policy["daily_limit"]) - int((created_today or {}).get("count", 0)))
    result["daily_remaining"] = remaining
    if not remaining:
        return result

    candidates = rows(
        """
        SELECT jobs.id, jobs.title, jobs.company, jobs.score
        FROM jobs
        WHERE jobs.status = 'new'
          AND jobs.score >= ?
          AND NOT EXISTS(
            SELECT 1 FROM pipeline_items WHERE pipeline_items.job_id = jobs.id
          )
          AND NOT EXISTS(
            SELECT 1 FROM applications WHERE applications.job_id = jobs.id
          )
        ORDER BY jobs.score DESC, jobs.discovered_at, jobs.id
        LIMIT ?
        """,
        (int(policy["minimum_score"]), remaining),
    )
    result["eligible"] = len(candidates)
    now = now_iso()
    for job in candidates:
        message = (
            f"Queued {job['title']} at {job['company']} for automatic application preparation."
        )
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO pipeline_items(
                  job_id, status, stage, message, policy_json, created_at, updated_at
                )
                VALUES(?, 'queued', 'discovered', ?, ?, ?, ?)
                """,
                (int(job["id"]), message, json.dumps(policy), now, now),
            )
            if not cursor.rowcount:
                continue
            item_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE id = ?",
                (now, int(job["id"])),
            )
        result["queued"] += 1
        _record_event(
            item_id,
            "queued",
            "discovered",
            message,
            {"job_id": int(job["id"]), "score": int(job["score"])},
        )
    result["daily_remaining"] = max(0, remaining - result["queued"])
    return result


def _latest_application(job_id: int) -> dict[str, Any] | None:
    return row(
        "SELECT * FROM applications WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    )


def _latest_writing_task(application_id: int) -> dict[str, Any] | None:
    return row(
        "SELECT * FROM writing_tasks WHERE application_id = ? ORDER BY id DESC LIMIT 1",
        (application_id,),
    )


def _latest_browser_task(application_id: int) -> dict[str, Any] | None:
    return row(
        "SELECT * FROM application_tasks WHERE application_id = ? ORDER BY id DESC LIMIT 1",
        (application_id,),
    )


def _current_writing_origin(application_id: int) -> str:
    found = row(
        """
        SELECT writing_versions.origin
        FROM applications
        LEFT JOIN writing_versions
          ON writing_versions.id = applications.current_writing_version_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )
    return str((found or {}).get("origin") or "")


def _failed(
    item: dict[str, Any],
    stage: str,
    exc: Exception,
    *,
    blocked: bool = False,
) -> dict[str, Any]:
    message = str(exc)
    status = "blocked" if blocked else "failed"
    prefix = "Pipeline is waiting" if blocked else "Pipeline step failed"
    _transition(
        int(item["id"]),
        status,
        stage,
        f"{prefix}: {message}",
        error=message,
    )
    return get_item(int(item["id"])) or {}


def advance_item(
    item_id: int,
    *,
    draft_fn: Callable[[int, str | None], dict[str, object]] | None = None,
    queue_writer_fn: Callable[[int], dict[str, object]] | None = None,
    approve_fn: Callable[[int], None] | None = None,
    apply_fn: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    with _process_lock:
        item = row("SELECT * FROM pipeline_items WHERE id = ?", (item_id,))
        if not item or item["status"] in TERMINAL_STATUSES:
            return get_item(item_id)
        policy = _policy_from_item(item)
        job_id = int(item["job_id"])
        application = (
            row("SELECT * FROM applications WHERE id = ?", (int(item["application_id"]),))
            if item["application_id"]
            else _latest_application(job_id)
        )
        retrying = item["status"] == "queued" and item["stage"] == "reconcile"

        if application and not item["application_id"]:
            _transition(
                item_id,
                "queued",
                "reconcile",
                "Found the existing application package and resumed pipeline tracking.",
                application_id=int(application["id"]),
            )
            item = row("SELECT * FROM pipeline_items WHERE id = ?", (item_id,)) or item

        if not application:
            _increment_attempt(item_id)
            _transition(
                item_id,
                "running",
                "drafting",
                "Creating a grounded application package and compiling its LaTeX resume.",
            )
            try:
                create = draft_fn or applications.draft_application
                application = create(job_id, str(policy.get("mode") or "review"))
            except Exception as exc:
                blocked = "ingest at least one source document" in str(exc).lower()
                return _failed(item, "documents_required" if blocked else "drafting", exc, blocked=blocked)
            _transition(
                item_id,
                "queued",
                "package_ready",
                "Grounded application package created and LaTeX compilation attempted.",
                application_id=int(application["id"]),
            )
            return get_item(item_id)

        application_id = int(application["id"])
        if application["status"] == "submitted":
            _transition(
                item_id,
                "submitted",
                "submitted",
                "Application submission is recorded as complete.",
                application_id=application_id,
            )
            return get_item(item_id)

        browser_task = _latest_browser_task(application_id)
        if browser_task and not (retrying and browser_task["status"] in {"failed", "cancelled"}):
            if browser_task["status"] == "submitted":
                _transition(item_id, "submitted", "submitted", str(browser_task["message"]))
                return get_item(item_id)
            if browser_task["status"] == "checkpoint":
                _transition(
                    item_id,
                    "checkpoint",
                    str(browser_task["checkpoint_kind"] or "browser_checkpoint"),
                    str(browser_task["message"]),
                )
                return get_item(item_id)
            if browser_task["status"] in {"queued", "running"}:
                _transition(
                    item_id,
                    "applying",
                    str(browser_task["current_step"] or "browser"),
                    str(browser_task["message"]),
                )
                return get_item(item_id)
            if browser_task["status"] == "failed":
                return _failed(item, "browser", RuntimeError(str(browser_task["message"])))
            if browser_task["status"] == "cancelled":
                _transition(item_id, "cancelled", "cancelled", str(browser_task["message"]))
                return get_item(item_id)

        writing_task = _latest_writing_task(application_id)
        if writing_task and not (retrying and writing_task["status"] == "failed"):
            if writing_task["status"] in {"queued", "running"}:
                _transition(
                    item_id,
                    "writing",
                    str(writing_task["status"]),
                    str(writing_task["message"]),
                )
                return get_item(item_id)
            if writing_task["status"] == "failed":
                return _failed(item, "writing", RuntimeError(str(writing_task["message"])))

        origin = _current_writing_origin(application_id)
        if policy.get("auto_write") and origin != "codex":
            _increment_attempt(item_id)
            _transition(
                item_id,
                "running",
                "queue_writing",
                "Queueing Codex to tailor the resume, cover letter, statements, and outreach text.",
            )
            try:
                queue = queue_writer_fn or writing.queue_codex_draft
                task = queue(application_id)
            except Exception as exc:
                return _failed(item, "codex_unavailable", exc, blocked=True)
            _transition(
                item_id,
                "writing",
                "queued",
                str(task.get("message") or "Codex writing task queued."),
            )
            return get_item(item_id)

        application = row("SELECT * FROM applications WHERE id = ?", (application_id,)) or application
        if application["resume_compile_status"] != "compiled":
            if retrying:
                try:
                    application = applications.recompile_application(application_id)
                except Exception as exc:
                    return _failed(item, "latex_compilation", exc)
            if application["resume_compile_status"] != "compiled":
                return _failed(
                    item,
                    "latex_compilation",
                    RuntimeError(str(application["resume_compile_message"] or "LaTeX PDF did not compile")),
                )

        mode = str(application["mode"] or policy.get("mode") or "review")
        if application["status"] == "approved":
            if not policy.get("auto_apply"):
                _transition(
                    item_id,
                    "ready",
                    "ready_to_apply",
                    "Application materials are approved and ready for manual browser queueing.",
                )
                return get_item(item_id)
            _increment_attempt(item_id)
            _transition(
                item_id,
                "running",
                "queue_browser",
                "Approved materials are ready; queueing guarded browser automation.",
            )
            try:
                apply = apply_fn or automation.apply_application
                task = apply(application_id)
            except Exception as exc:
                return _failed(item, "queue_browser", exc)
            if task.get("status") == "blocked":
                return _failed(
                    item,
                    "queue_browser",
                    RuntimeError(str(task.get("message") or "Browser automation was blocked")),
                    blocked=True,
                )
            _transition(
                item_id,
                "applying",
                str(task.get("current_step") or task.get("status") or "queued"),
                str(task.get("message") or "Browser application queued."),
            )
            return get_item(item_id)

        if mode == "rules_autonomous" and policy.get("auto_approve"):
            _increment_attempt(item_id)
            _transition(
                item_id,
                "running",
                "approval",
                "Evidence-validated materials passed the explicit rules-autonomous approval policy.",
            )
            try:
                approve = approve_fn or applications.approve_application
                approve(application_id)
            except Exception as exc:
                return _failed(item, "approval", exc)
            _transition(
                item_id,
                "approved",
                "approved",
                "Application materials were approved by the configured rules-autonomous policy.",
            )
            return get_item(item_id)

        if mode == "assisted_autonomous" and policy.get("auto_apply"):
            _increment_attempt(item_id)
            _transition(
                item_id,
                "running",
                "queue_browser",
                "Validated materials are ready; queueing assisted browser automation.",
            )
            try:
                apply = apply_fn or automation.apply_application
                task = apply(application_id)
            except Exception as exc:
                return _failed(item, "queue_browser", exc)
            if task.get("status") == "blocked":
                return _failed(
                    item,
                    "queue_browser",
                    RuntimeError(str(task.get("message") or "Browser automation was blocked")),
                    blocked=True,
                )
            _transition(
                item_id,
                "applying",
                str(task.get("current_step") or task.get("status") or "queued"),
                str(task.get("message") or "Browser application queued."),
            )
            return get_item(item_id)

        _transition(
            item_id,
            "review",
            "materials_review",
            "Tailored materials and the compiled LaTeX resume are ready for review.",
        )
        return get_item(item_id)


def process_cycle(limit: int = 20) -> dict[str, int]:
    summary = {"queued": 0, "advanced": 0, "checked": 0}
    enqueue_result = enqueue_eligible_jobs()
    summary["queued"] = enqueue_result["queued"]
    candidates = rows(
        """
        SELECT id, status, stage, updated_at
        FROM pipeline_items
        WHERE status NOT IN ('submitted', 'skipped', 'cancelled', 'blocked', 'failed')
        ORDER BY updated_at, id
        LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )
    for candidate in candidates:
        before = (candidate["status"], candidate["stage"], candidate["updated_at"])
        current = advance_item(int(candidate["id"]))
        summary["checked"] += 1
        if current and (current["status"], current["stage"], current["updated_at"]) != before:
            summary["advanced"] += 1
    return summary


def retry_item(item_id: int) -> dict[str, Any]:
    item = row("SELECT * FROM pipeline_items WHERE id = ?", (item_id,))
    if not item:
        raise ValueError("Pipeline item does not exist")
    if item["status"] not in PAUSED_STATUSES:
        raise ValueError("Only blocked or failed pipeline items can be retried")
    with connect() as conn:
        conn.execute(
            """
            UPDATE pipeline_items
            SET status = 'queued', stage = 'reconcile', message = ?,
                last_error = '', attempt_count = attempt_count + 1,
                updated_at = ?, completed_at = ''
            WHERE id = ?
            """,
            ("Retry requested. Rechecking the application from its last durable state.", now_iso(), item_id),
        )
    _record_event(
        item_id,
        "queued",
        "reconcile",
        "Retry requested. Rechecking the application from its last durable state.",
    )
    _worker_event.set()
    return get_item(item_id) or {}


def skip_item(item_id: int) -> dict[str, Any]:
    item = row("SELECT * FROM pipeline_items WHERE id = ?", (item_id,))
    if not item:
        raise ValueError("Pipeline item does not exist")
    if item["status"] in TERMINAL_STATUSES:
        raise ValueError("This pipeline item is already complete")
    _transition(
        item_id,
        "skipped",
        "skipped",
        "Application pipeline skipped by the user.",
    )
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'skipped', updated_at = ? WHERE id = ?",
            (now_iso(), int(item["job_id"])),
        )
    return get_item(item_id) or {}


def get_item(item_id: int) -> dict[str, Any] | None:
    items = list_items(item_id=item_id)
    return items[0] if items else None


def list_items(limit: int = 100, item_id: int | None = None) -> list[dict[str, Any]]:
    where = "WHERE pipeline_items.id = ?" if item_id is not None else ""
    params: tuple[object, ...] = (item_id,) if item_id is not None else (max(1, min(limit, 500)),)
    limit_clause = "" if item_id is not None else "LIMIT ?"
    items = rows(
        f"""
        SELECT pipeline_items.*, jobs.title, jobs.company, jobs.score, jobs.source,
               applications.status AS application_status,
               applications.mode AS application_mode,
               applications.resume_compile_status,
               applications.writing_status
        FROM pipeline_items
        JOIN jobs ON jobs.id = pipeline_items.job_id
        LEFT JOIN applications ON applications.id = pipeline_items.application_id
        {where}
        ORDER BY pipeline_items.updated_at DESC, pipeline_items.id DESC
        {limit_clause}
        """,
        params,
    )
    for item in items:
        item["policy"] = _policy_from_item(item)
        item["events"] = rows(
            """
            SELECT id, status, stage, message, meta, created_at
            FROM pipeline_events
            WHERE pipeline_item_id = ?
            ORDER BY id DESC LIMIT 20
            """,
            (int(item["id"]),),
        )
        for event in item["events"]:
            try:
                event["meta"] = json.loads(str(event.get("meta") or "{}"))
            except json.JSONDecodeError:
                event["meta"] = {}
    return items


def pipeline_status() -> dict[str, object]:
    counts = rows(
        "SELECT status, COUNT(*) AS count FROM pipeline_items GROUP BY status ORDER BY status"
    )
    active = sum(int(item["count"]) for item in counts if item["status"] in ACTIVE_STATUSES)
    attention = sum(int(item["count"]) for item in counts if item["status"] in PAUSED_STATUSES | {"checkpoint"})
    return {
        "policy": pipeline_policy(),
        "counts": {str(item["status"]): int(item["count"]) for item in counts},
        "active": active,
        "attention": attention,
        "total": sum(int(item["count"]) for item in counts),
    }


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        with connect() as conn:
            recovered = conn.execute(
                """
                UPDATE pipeline_items
                SET status = 'queued', stage = 'reconcile', message = ?,
                    updated_at = ?
                WHERE status = 'running'
                """,
                ("Recovered pipeline work after a local server restart.", now_iso()),
            ).rowcount
        if recovered:
            log(f"Recovered {recovered} pipeline item(s) after a local server restart.", "warning")
        _worker_started = True

        def loop() -> None:
            while True:
                try:
                    result = process_cycle()
                except Exception as exc:
                    log(f"Pipeline worker error: {exc}", "error")
                    result = {"queued": 0, "advanced": 0}
                if not result["queued"] and not result["advanced"]:
                    _worker_event.wait(timeout=3)
                    _worker_event.clear()

        threading.Thread(
            target=loop,
            daemon=True,
            name="applyforme-pipeline-worker",
        ).start()
