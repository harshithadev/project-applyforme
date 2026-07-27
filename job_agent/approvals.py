from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import (
    applications,
    automation,
    browser_sessions,
    documents,
    orchestration,
    outreach,
)
from .db import connect, log, now_iso, row, rows, setting


Notifier = Callable[[str, str], None]
MAX_NOTIFICATION_ATTEMPTS = 3

_worker_started = False
_worker_lock = threading.Lock()
_worker_event = threading.Event()


def _json_value(value: object, fallback: object) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError):
        return fallback


def _action(
    action_id: str,
    label: str,
    *,
    style: str = "primary",
    confirmation: str = "",
) -> dict[str, str]:
    return {
        "id": action_id,
        "label": label,
        "style": style,
        "confirmation": confirmation,
    }


def _application_candidates() -> list[dict[str, object]]:
    found = rows(
        """
        SELECT applications.id, applications.current_writing_version_id,
               applications.resume_compile_status, applications.updated_at,
               jobs.title, jobs.company
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.status = 'drafted'
          AND applications.writing_status = 'draft'
          AND applications.current_writing_version_id IS NOT NULL
          AND applications.resume_compile_status = 'compiled'
        """
    )
    return [
        {
            "dedupe_key": (
                f"application-review:{item['id']}:"
                f"v{item['current_writing_version_id']}"
            ),
            "kind": "application_review",
            "source_type": "application",
            "source_id": int(item["id"]),
            "application_id": int(item["id"]),
            "priority": 85,
            "title": f"Review {item['title']} at {item['company']}",
            "summary": "The tailored resume and written materials are compiled and ready for approval.",
            "actions": [
                _action("approve", "Approve package"),
                _action(
                    "skip",
                    "Skip application",
                    style="secondary",
                    confirmation="Skip this application and cancel its queued work?",
                ),
            ],
            "payload": {
                "writing_version_id": int(item["current_writing_version_id"]),
                "resume_compile_status": item["resume_compile_status"],
                "resume_url": (
                    f"/api/applications/artifact?application_id={item['id']}&kind=pdf"
                ),
            },
            "source_updated_at": item["updated_at"],
        }
        for item in found
    ]


def _browser_actions(kind: str) -> list[dict[str, str]]:
    cancel = _action(
        "cancel",
        "Cancel task",
        style="secondary",
        confirmation="Cancel this browser application task?",
    )
    if kind == "final_review":
        return [
            _action(
                "submit",
                "Submit application",
                style="warning",
                confirmation="Submit this application now? This is the final external action.",
            ),
            cancel,
        ]
    if kind in {"unknown_field", "sensitive_question"}:
        return [_action("continue", "Save answers and continue"), cancel]
    if kind == "daily_limit":
        return [_action("continue", "Retry after limit resets"), cancel]
    if kind == "submission_uncertain":
        return [
            _action(
                "mark_submitted",
                "Mark submitted",
                confirmation="Only mark submitted after verifying the employer confirmation.",
            ),
            cancel,
        ]
    if kind == "login":
        return [_action("sign_in", "Open sign-in window"), cancel]
    if kind in browser_sessions.MANUAL_TAKEOVER_KINDS:
        return [_action("manual_takeover", "Open manual browser"), cancel]
    return [cancel]


def _browser_priority(kind: str) -> int:
    if kind in {"final_review", "submission_uncertain"}:
        return 100
    if kind == "sensitive_question":
        return 95
    if kind == "unknown_field":
        return 90
    if kind in {"captcha", "login"}:
        return 80
    return 70


def _browser_candidates() -> list[dict[str, object]]:
    found = rows(
        """
        SELECT application_tasks.*, jobs.title, jobs.company
        FROM application_tasks
        JOIN applications ON applications.id = application_tasks.application_id
        JOIN jobs ON jobs.id = applications.job_id
        WHERE application_tasks.status = 'checkpoint'
        """
    )
    candidates: list[dict[str, object]] = []
    for item in found:
        checkpoint = _json_value(item.get("checkpoint_json"), {})
        screenshots = _json_value(item.get("screenshots_json"), [])
        kind = str(item.get("checkpoint_kind") or "review")
        screenshot_name = str(screenshots[-1]) if screenshots else ""
        payload = {
            "checkpoint_kind": kind,
            "fields": checkpoint.get("fields", []) if isinstance(checkpoint, dict) else [],
            "target_url": (
                checkpoint.get("target_url", item["target_url"])
                if isinstance(checkpoint, dict)
                else item["target_url"]
            ),
            "screenshot_name": screenshot_name,
            "screenshot_url": (
                f"/api/applications/task-artifact?task_id={item['id']}"
                f"&name={screenshot_name}"
                if screenshot_name
                else ""
            ),
        }
        candidates.append(
            {
                "dedupe_key": (
                    f"browser-checkpoint:{item['id']}:{kind}:{item['updated_at']}"
                ),
                "kind": "browser_checkpoint",
                "source_type": "browser_task",
                "source_id": int(item["id"]),
                "application_id": int(item["application_id"]),
                "priority": _browser_priority(kind),
                "title": f"Browser action for {item['title']} at {item['company']}",
                "summary": item["message"],
                "actions": _browser_actions(kind),
                "payload": payload,
                "source_updated_at": item["updated_at"],
            }
        )
    return candidates


def _outreach_candidates() -> list[dict[str, object]]:
    if setting("email_mode", "approval") != "approval":
        return []
    found = rows(
        """
        SELECT outreach_threads.id, outreach_threads.application_id,
               outreach_threads.active_revision_id, outreach_threads.updated_at,
               outreach_threads.recipient_email, contacts.name AS contact_name,
               jobs.title, jobs.company, outreach_revisions.subject, outreach_revisions.body
        FROM outreach_threads
        JOIN contacts ON contacts.id = outreach_threads.contact_id
        JOIN applications ON applications.id = outreach_threads.application_id
        JOIN jobs ON jobs.id = applications.job_id
        JOIN outreach_revisions
          ON outreach_revisions.id = outreach_threads.active_revision_id
        WHERE outreach_threads.status = 'draft'
        """
    )
    return [
        {
            "dedupe_key": (
                f"outreach-review:{item['id']}:r{item['active_revision_id']}"
            ),
            "kind": "outreach_review",
            "source_type": "outreach",
            "source_id": int(item["id"]),
            "application_id": int(item["application_id"]),
            "priority": 75,
            "title": (
                f"Review outreach to "
                f"{item['contact_name'] or item['recipient_email']}"
            ),
            "summary": f"Message for {item['title']} at {item['company']} is ready for approval.",
            "actions": [
                _action("approve", "Approve message"),
                _action(
                    "dismiss",
                    "Dismiss draft",
                    style="secondary",
                    confirmation="Dismiss this outreach draft?",
                ),
            ],
            "payload": {
                "recipient_email": item["recipient_email"],
                "subject": item["subject"],
                "body": item["body"],
            },
            "source_updated_at": item["updated_at"],
        }
        for item in found
    ]


def _pipeline_candidates() -> list[dict[str, object]]:
    found = rows(
        """
        SELECT pipeline_items.*, jobs.title, jobs.company
        FROM pipeline_items
        JOIN jobs ON jobs.id = pipeline_items.job_id
        WHERE pipeline_items.status IN ('blocked', 'failed')
        """
    )
    return [
        {
            "dedupe_key": (
                f"pipeline-attention:{item['id']}:{item['status']}:{item['updated_at']}"
            ),
            "kind": "pipeline_attention",
            "source_type": "pipeline",
            "source_id": int(item["id"]),
            "application_id": (
                int(item["application_id"]) if item["application_id"] else None
            ),
            "priority": 80 if item["status"] == "failed" else 70,
            "title": f"Pipeline needs attention for {item['title']} at {item['company']}",
            "summary": item["message"] or item["last_error"],
            "actions": [
                _action("retry", "Retry"),
                _action(
                    "skip",
                    "Skip application",
                    style="secondary",
                    confirmation="Skip this pipeline item and cancel its queued work?",
                ),
            ],
            "payload": {
                "stage": item["stage"],
                "status": item["status"],
                "last_error": item["last_error"],
            },
            "source_updated_at": item["updated_at"],
        }
        for item in found
    ]


def _document_candidates() -> list[dict[str, object]]:
    found = rows(
        """
        SELECT id, name, kind, ingest_status, ingest_error, sha256, extractor,
               extraction_confidence, classification_confidence, content,
               updated_at
        FROM documents
        WHERE ingest_status IN ('ready', 'pending_review', 'error', 'duplicate')
        """
    )
    candidates: list[dict[str, object]] = []
    for item in found:
        status = str(item["ingest_status"])
        if status == "pending_review":
            priority = 75
            title = f"Review document {item['name']}"
            summary = "Extracted document evidence is waiting for approval before it updates your profile."
            actions = [
                _action("approve", "Approve evidence"),
                _action("archive", "Archive", style="secondary"),
                _action(
                    "remove",
                    "Remove",
                    style="secondary",
                    confirmation="Permanently remove this document from local storage?",
                ),
            ]
        elif status in {"error", "duplicate"}:
            priority = 80 if status == "error" else 45
            title = f"Document needs attention: {item['name']}"
            summary = str(item["ingest_error"] or "Document ingestion needs review.")
            actions = [
                _action("retry", "Retry extraction"),
                _action("archive", "Archive", style="secondary"),
                _action(
                    "remove",
                    "Remove",
                    style="secondary",
                    confirmation="Permanently remove this document from local storage?",
                ),
            ]
        else:
            priority = 35
            title = f"Document ingested: {item['name']}"
            summary = "The document was added to your source-grounded candidate profile."
            actions = [
                _action("acknowledge", "Acknowledge"),
                _action("archive", "Archive", style="secondary"),
                _action(
                    "remove",
                    "Remove",
                    style="secondary",
                    confirmation="Permanently remove this document from local storage?",
                ),
            ]
        candidates.append(
            {
                "dedupe_key": f"document:{item['id']}:{item['sha256']}",
                "kind": "document_review",
                "source_type": "document",
                "source_id": int(item["id"]),
                "application_id": None,
                "priority": priority,
                "title": title,
                "summary": summary,
                "actions": actions,
                "payload": {
                    "document_kind": item["kind"],
                    "ingest_status": status,
                    "extractor": item["extractor"],
                    "extraction_confidence": item["extraction_confidence"],
                    "classification_confidence": item["classification_confidence"],
                    "content_preview": str(item["content"] or "")[:2_000],
                    "artifact_url": (
                        f"/api/documents/artifact?document_id={item['id']}"
                    ),
                },
                "source_updated_at": item["updated_at"],
            }
        )
    return candidates


def _candidates() -> list[dict[str, object]]:
    return [
        *_application_candidates(),
        *_browser_candidates(),
        *_outreach_candidates(),
        *_pipeline_candidates(),
        *_document_candidates(),
    ]


def sync_inbox() -> dict[str, int]:
    candidates = _candidates()
    active_keys = {str(item["dedupe_key"]) for item in candidates}
    now = now_iso()
    created = 0
    resolved = 0
    with connect() as conn:
        for item in candidates:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO approval_items(
                  dedupe_key, kind, source_type, source_id, application_id,
                  priority, title, summary, status, actions_json, payload_json,
                  source_updated_at, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    item["dedupe_key"],
                    item["kind"],
                    item["source_type"],
                    item["source_id"],
                    item["application_id"],
                    item["priority"],
                    item["title"],
                    item["summary"],
                    json.dumps(item["actions"]),
                    json.dumps(item["payload"]),
                    item["source_updated_at"],
                    now,
                    now,
                ),
            )
            created += int(cursor.rowcount or 0)
            conn.execute(
                """
                UPDATE approval_items
                SET priority = ?, title = ?, summary = ?, actions_json = ?,
                    payload_json = ?, source_updated_at = ?
                WHERE dedupe_key = ? AND status = 'pending'
                """,
                (
                    item["priority"],
                    item["title"],
                    item["summary"],
                    json.dumps(item["actions"]),
                    json.dumps(item["payload"]),
                    item["source_updated_at"],
                    item["dedupe_key"],
                ),
            )
        pending = conn.execute(
            """
            SELECT id, dedupe_key FROM approval_items
            WHERE status = 'pending'
              AND source_type IN (
                'application', 'browser_task', 'outreach', 'pipeline', 'document'
              )
            """
        ).fetchall()
        stale_ids = [
            int(item["id"])
            for item in pending
            if str(item["dedupe_key"]) not in active_keys
        ]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            resolved = conn.execute(
                f"""
                UPDATE approval_items
                SET status = 'resolved', resolution = 'source_changed',
                    resolved_at = ?, updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'pending'
                """,
                (now, now, *stale_ids),
            ).rowcount
    if created:
        _worker_event.set()
    return {"created": created, "resolved": int(resolved or 0)}


def _approval_item(item: dict[str, object]) -> dict[str, object]:
    result = dict(item)
    result["actions"] = _json_value(result.pop("actions_json", "[]"), [])
    result["payload"] = _json_value(result.pop("payload_json", "{}"), {})
    return result


def get_item(item_id: int) -> dict[str, object] | None:
    found = row("SELECT * FROM approval_items WHERE id = ?", (item_id,))
    return _approval_item(found) if found else None


def list_items(status: str = "pending", limit: int = 100) -> list[dict[str, object]]:
    allowed = {"pending", "resolved", "all"}
    if status not in allowed:
        raise ValueError("Approval status must be pending, resolved, or all")
    where = "" if status == "all" else "WHERE status = ?"
    params: tuple[object, ...] = (
        (max(1, min(limit, 500)),)
        if status == "all"
        else (status, max(1, min(limit, 500)))
    )
    return [
        _approval_item(item)
        for item in rows(
            f"""
            SELECT * FROM approval_items
            {where}
            ORDER BY
              CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
              priority DESC, created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
    ]


def decision_history(limit: int = 100) -> list[dict[str, object]]:
    found = rows(
        """
        SELECT approval_decisions.*, approval_items.kind, approval_items.title,
               approval_items.application_id
        FROM approval_decisions
        JOIN approval_items ON approval_items.id = approval_decisions.approval_item_id
        ORDER BY approval_decisions.created_at DESC, approval_decisions.id DESC
        LIMIT ?
        """,
        (max(1, min(limit, 500)),),
    )
    for item in found:
        item["payload"] = _json_value(item.pop("payload_json", "{}"), {})
    return found


def _notification_counts() -> dict[str, int]:
    return {
        str(item["status"]): int(item["count"])
        for item in rows(
            "SELECT status, COUNT(*) AS count FROM notification_deliveries GROUP BY status"
        )
    }


def _parse_minutes(value: str, fallback: int) -> int:
    try:
        hour, minute = value.split(":", 1)
        parsed = int(hour) * 60 + int(minute)
        if 0 <= parsed < 24 * 60 and 0 <= int(minute) < 60:
            return parsed
    except (ValueError, AttributeError):
        pass
    return fallback


def is_quiet_time(current_time: datetime | None = None) -> bool:
    current = current_time or datetime.now().astimezone()
    minute = current.hour * 60 + current.minute
    start = _parse_minutes(setting("notification_quiet_start", "22:00"), 22 * 60)
    end = _parse_minutes(setting("notification_quiet_end", "08:00"), 8 * 60)
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def notification_status(current_time: datetime | None = None) -> dict[str, object]:
    return {
        "supported": sys.platform == "darwin",
        "enabled": setting("notifications_enabled", "true") == "true",
        "quiet": is_quiet_time(current_time),
        "quiet_start": setting("notification_quiet_start", "22:00"),
        "quiet_end": setting("notification_quiet_end", "08:00"),
        "deliveries": _notification_counts(),
    }


def inbox_state() -> dict[str, object]:
    sync_inbox()
    items = list_items()
    counts: dict[str, int] = {}
    for item in items:
        kind = str(item["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "items": items,
        "history": decision_history(50),
        "summary": {
            "pending": len(items),
            "urgent": sum(1 for item in items if int(item["priority"]) >= 90),
            "by_kind": counts,
        },
        "notifications": notification_status(),
    }


def _skip_application(application_id: int) -> None:
    pipeline = row(
        """
        SELECT id, status FROM pipeline_items
        WHERE application_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (application_id,),
    )
    if pipeline and pipeline["status"] not in {"submitted", "skipped", "cancelled"}:
        orchestration.skip_item(int(pipeline["id"]))
        return
    for task in rows(
        """
        SELECT id FROM application_tasks
        WHERE application_id = ? AND status IN ('queued', 'checkpoint')
        """,
        (application_id,),
    ):
        automation.cancel_task(int(task["id"]))
    with connect() as conn:
        conn.execute(
            "UPDATE applications SET status = 'skipped', updated_at = ? WHERE id = ?",
            (now_iso(), application_id),
        )
        conn.execute(
            """
            UPDATE jobs SET status = 'skipped', updated_at = ?
            WHERE id = (SELECT job_id FROM applications WHERE id = ?)
            """,
            (now_iso(), application_id),
        )
    log(f"Skipped application {application_id} from the approval inbox.", "warning")


def _dismiss_outreach(thread_id: int) -> None:
    with connect() as conn:
        changed = conn.execute(
            """
            UPDATE outreach_threads
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND status = 'draft'
            """,
            (now_iso(), thread_id),
        ).rowcount
    if not changed:
        raise ValueError("Only draft outreach can be dismissed")
    log(f"Dismissed outreach thread {thread_id} from the approval inbox.", "warning")


def _required_answers(item: dict[str, object], payload: dict[str, object]) -> dict[str, object]:
    item_payload = item.get("payload")
    fields = item_payload.get("fields", []) if isinstance(item_payload, dict) else []
    answers = payload.get("answers", {})
    if not isinstance(answers, dict):
        raise ValueError("Checkpoint answers must be an object")
    missing = [
        str(field.get("question") or "").strip()
        for field in fields
        if isinstance(field, dict)
        and str(field.get("question") or "").strip()
        and not str(answers.get(str(field.get("question") or ""), "")).strip()
    ]
    if missing:
        raise ValueError(f"Answer every required field before continuing: {', '.join(missing)}")
    return answers


def _dispatch(
    item: dict[str, object],
    action: str,
    payload: dict[str, object],
) -> dict[str, object]:
    source_type = str(item["source_type"])
    source_id = int(item["source_id"])
    if source_type == "application":
        if action == "approve":
            applications.approve_application(source_id)
            return {"status": "approved"}
        if action == "skip":
            _skip_application(source_id)
            return {"status": "skipped"}
    elif source_type == "outreach":
        if action == "approve":
            result = outreach.approve(source_id)
            return {"status": result["status"]}
        if action == "dismiss":
            _dismiss_outreach(source_id)
            return {"status": "cancelled"}
    elif source_type == "browser_task":
        if action == "sign_in":
            result = browser_sessions.start_login_handoff(source_id)
            return {"status": result["status"]}
        if action == "manual_takeover":
            result = browser_sessions.start_manual_takeover(source_id)
            return {"status": result["status"]}
        if action == "continue":
            answers = _required_answers(item, payload)
            result = automation.resolve_checkpoint(
                source_id,
                answers,
                approve_submit=False,
                save_rules=bool(payload.get("save_rules", True)),
            )
            return {"status": result["status"], "answer_count": len(answers)}
        if action == "submit":
            result = automation.resolve_checkpoint(
                source_id,
                approve_submit=True,
            )
            return {"status": result["status"]}
        if action == "cancel":
            result = automation.cancel_task(source_id)
            return {"status": result["status"]}
        if action == "mark_submitted":
            application_id = int(item.get("application_id") or 0)
            applications.mark_application_submitted(application_id)
            return {"status": "submitted"}
    elif source_type == "pipeline":
        if action == "retry":
            result = orchestration.retry_item(source_id)
            return {"status": result["status"]}
        if action == "skip":
            result = orchestration.skip_item(source_id)
            return {"status": result["status"]}
    elif source_type == "document":
        if action == "acknowledge":
            return {"status": "acknowledged"}
        if action == "approve":
            result = documents.approve_document(source_id, close_inbox=False)
            return {"status": result["ingest_status"]}
        if action == "retry":
            result = documents.retry_document(source_id, close_inbox=False)
            return {"status": result["ingest_status"]}
        if action == "archive":
            result = documents.archive_document(source_id, close_inbox=False)
            return {"status": result["ingest_status"]}
        if action == "remove":
            return documents.remove_document(source_id, close_inbox=False)
    raise ValueError(f"Action {action} is not supported for this approval item")


def resolve_item(
    item_id: int,
    action: str,
    payload: dict[str, object] | None = None,
    note: str = "",
) -> dict[str, object]:
    item = get_item(item_id)
    if not item:
        raise ValueError("Approval item does not exist")
    if item["status"] != "pending":
        raise ValueError("This approval item has already been resolved")
    allowed = {
        str(candidate.get("id"))
        for candidate in item.get("actions", [])
        if isinstance(candidate, dict)
    }
    if action not in allowed:
        raise ValueError("That action is not available for this approval item")
    clean_payload = payload if isinstance(payload, dict) else {}
    result = _dispatch(item, action, clean_payload)
    now = now_iso()
    decision_payload = {
        "result": result,
        "answer_count": len(clean_payload.get("answers", {}))
        if isinstance(clean_payload.get("answers"), dict)
        else 0,
    }
    with connect() as conn:
        changed = conn.execute(
            """
            UPDATE approval_items
            SET status = 'resolved', resolution = ?, resolved_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (action, now, now, item_id),
        ).rowcount
        if not changed:
            raise ValueError("This approval item was resolved by another action")
        conn.execute(
            """
            INSERT INTO approval_decisions(
              approval_item_id, action, note, payload_json, created_at
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                item_id,
                action,
                str(note or "").strip()[:1_000],
                json.dumps(decision_payload),
                now,
            ),
        )
    log(
        f"Resolved approval item {item_id} with action {action}.",
        meta={
            "approval_item_id": item_id,
            "source_type": item["source_type"],
            "source_id": item["source_id"],
            "action": action,
        },
    )
    return {
        "ok": True,
        "item": get_item(item_id),
        "result": result,
        "inbox": inbox_state(),
    }


def _apple_string(value: object) -> str:
    clean = " ".join(str(value or "").split())
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos_notifier(title: str, message: str) -> None:
    script = (
        f"display notification {_apple_string(message[:240])} "
        f"with title {_apple_string(title[:80])}"
    )
    completed = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "macOS notification delivery failed"
        )


def send_pending_notifications(
    notifier: Notifier | None = None,
    current_time: datetime | None = None,
) -> dict[str, object]:
    sync_inbox()
    status = notification_status(current_time)
    if not status["enabled"]:
        return {"status": "disabled", "sent": 0, "failed": 0}
    if status["quiet"]:
        return {"status": "quiet", "sent": 0, "failed": 0}
    if notifier is None and not status["supported"]:
        return {"status": "unsupported", "sent": 0, "failed": 0}
    sender = notifier or _macos_notifier
    pending = rows(
        """
        SELECT approval_items.id, approval_items.dedupe_key,
               approval_items.title, approval_items.summary
        FROM approval_items
        WHERE approval_items.status = 'pending'
        ORDER BY approval_items.priority DESC, approval_items.created_at
        """
    )
    sent = 0
    failed = 0
    for item in pending:
        delivery_key = f"approval:{item['dedupe_key']}"
        now = now_iso()
        with connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries(
                  approval_item_id, dedupe_key, channel, status, message,
                  created_at, updated_at
                )
                VALUES(?, ?, 'macos', 'queued', ?, ?, ?)
                """,
                (item["id"], delivery_key, item["summary"], now, now),
            )
            delivery = conn.execute(
                "SELECT * FROM notification_deliveries WHERE dedupe_key = ?",
                (delivery_key,),
            ).fetchone()
        if not delivery or delivery["status"] == "sent":
            continue
        if int(delivery["attempt_count"]) >= MAX_NOTIFICATION_ATTEMPTS:
            continue
        try:
            sender(str(item["title"]), str(item["summary"]))
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE notification_deliveries
                    SET status = 'sent', attempt_count = attempt_count + 1,
                        last_error = '', sent_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_iso(), now_iso(), int(delivery["id"])),
                )
            sent += 1
        except Exception as exc:
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE notification_deliveries
                    SET status = 'failed', attempt_count = attempt_count + 1,
                        last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), now_iso(), int(delivery["id"])),
                )
            failed += 1
    return {"status": "complete", "sent": sent, "failed": failed}


def send_test_notification(notifier: Notifier | None = None) -> dict[str, object]:
    status = notification_status()
    if not status["enabled"]:
        raise ValueError("Enable notifications before sending a test")
    if notifier is None and not status["supported"]:
        raise ValueError("Native notifications are currently supported on macOS only")
    (notifier or _macos_notifier)(
        "ApplyForMe notifications are ready",
        "New approvals and background failures will appear here.",
    )
    return {"ok": True, "message": "Test notification sent."}


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

        def loop() -> None:
            while True:
                try:
                    send_pending_notifications()
                except Exception as exc:
                    try:
                        log(f"Approval notification worker error: {exc}", "error")
                    except Exception:
                        pass
                _worker_event.wait(timeout=15)
                _worker_event.clear()

        threading.Thread(
            target=loop,
            daemon=True,
            name="applyforme-approval-worker",
        ).start()
