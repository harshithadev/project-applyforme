from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from . import browser_diagnostics
from .db import connect, log, now_iso, row, rows, setting


AUTO_RETRY_CATEGORIES = frozenset({"timeout", "network", "browser_environment"})
CIRCUIT_FAILURE_CATEGORIES = frozenset({"timeout", "network"})
NON_PROBE_CHECKPOINTS = frozenset({"daily_limit", "session_busy"})


def _integer_setting(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(setting(key, str(default)) or str(default))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def recovery_policy() -> dict[str, object]:
    return {
        "enabled": setting("browser_retry_enabled", "true") == "true",
        "max_attempts": _integer_setting("browser_retry_max_attempts", 3, 1, 10),
        "base_delay_seconds": _integer_setting(
            "browser_retry_base_delay_seconds", 60, 0, 3_600
        ),
        "max_delay_seconds": _integer_setting(
            "browser_retry_max_delay_seconds", 900, 0, 86_400
        ),
        "circuit_failure_threshold": _integer_setting(
            "browser_circuit_failure_threshold", 3, 1, 20
        ),
        "circuit_cooldown_minutes": _integer_setting(
            "browser_circuit_cooldown_minutes", 30, 1, 1_440
        ),
        "automatic_categories": sorted(AUTO_RETRY_CATEGORIES),
    }


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def task_hostname(task: dict[str, Any]) -> str:
    target = str(task.get("resume_url") or task.get("target_url") or "")
    return (urlparse(target).hostname or "").casefold()


def _circuit_dict(item: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    current = _utc_now(now)
    retry_after = _parse_iso(item.get("retry_after"))
    remaining = (
        max(0, int((retry_after - current).total_seconds()))
        if retry_after
        else 0
    )
    effective_status = str(item.get("status") or "closed")
    if effective_status == "open" and remaining == 0:
        effective_status = "probe_ready"
    item["effective_status"] = effective_status
    item["remaining_seconds"] = remaining
    return item


def get_circuit(adapter: str, hostname: str) -> dict[str, Any]:
    found = row(
        """
        SELECT * FROM adapter_circuit_breakers
        WHERE adapter = ? AND hostname = ?
        """,
        (adapter, hostname),
    )
    if found:
        return _circuit_dict(found)
    return _circuit_dict(
        {
            "adapter": adapter,
            "hostname": hostname,
            "status": "closed",
            "consecutive_failures": 0,
            "opened_at": "",
            "retry_after": "",
            "last_category": "",
            "last_message": "",
            "updated_at": "",
        }
    )


def attempt_gate(
    task: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, object]:
    current = _utc_now(now)
    circuit = get_circuit(str(task.get("adapter") or ""), task_hostname(task))
    status = str(circuit.get("status") or "closed")
    retry_after = _parse_iso(circuit.get("retry_after"))
    if status == "open" and retry_after and retry_after > current:
        return {
            "allowed": False,
            "reason": "circuit_open",
            "next_attempt_at": _iso(retry_after),
            "message": (
                f"The {task.get('adapter') or 'ATS'} circuit is open for "
                f"{task_hostname(task) or 'this host'} until {_iso(retry_after)}."
            ),
            "circuit": circuit,
        }
    if status == "half_open":
        next_check = current + timedelta(seconds=30)
        return {
            "allowed": False,
            "reason": "probe_in_progress",
            "next_attempt_at": _iso(next_check),
            "message": "A circuit recovery probe is already in progress for this ATS host.",
            "circuit": circuit,
        }
    return {"allowed": True, "reason": "", "next_attempt_at": "", "circuit": circuit}


def mark_attempt_started(task: dict[str, Any]) -> None:
    adapter = str(task.get("adapter") or "")
    hostname = task_hostname(task)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE adapter_circuit_breakers
            SET status = 'half_open', updated_at = ?
            WHERE adapter = ? AND hostname = ? AND status = 'open'
              AND (retry_after = '' OR retry_after <= ?)
            """,
            (now, adapter, hostname, now),
        )


def record_outcome(
    task: dict[str, Any],
    *,
    status: str,
    checkpoint_kind: str = "",
    message: str = "",
    manual: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    adapter = str(task.get("adapter") or "unsupported")
    hostname = task_hostname(task)
    current = _utc_now(now)
    classification = browser_diagnostics.classify_outcome(
        status,
        checkpoint_kind,
        message,
        manual,
    )
    existing = row(
        """
        SELECT * FROM adapter_circuit_breakers
        WHERE adapter = ? AND hostname = ?
        """,
        (adapter, hostname),
    )
    existing_status = str(existing.get("status") or "closed") if existing else "closed"
    failures = int(existing.get("consecutive_failures") or 0) if existing else 0
    circuit_status = existing_status
    opened_at = str(existing.get("opened_at") or "") if existing else ""
    retry_after = str(existing.get("retry_after") or "") if existing else ""

    if status == "submitted" or (
        status == "checkpoint" and checkpoint_kind not in NON_PROBE_CHECKPOINTS
    ):
        circuit_status = "closed"
        failures = 0
        opened_at = ""
        retry_after = ""
    elif status == "checkpoint" and existing_status == "half_open":
        circuit_status = "open"
        retry_after = _iso(current)
    elif status == "failed":
        category = str(classification["category"])
        if existing_status == "half_open" or category in CIRCUIT_FAILURE_CATEGORIES:
            failures += 1
            policy = recovery_policy()
            threshold = int(policy["circuit_failure_threshold"])
            if existing_status == "half_open" or failures >= threshold:
                circuit_status = "open"
                opened_at = _iso(current)
                retry_after = _iso(
                    current
                    + timedelta(minutes=int(policy["circuit_cooldown_minutes"]))
                )
            else:
                circuit_status = "closed"

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO adapter_circuit_breakers(
              adapter, hostname, status, consecutive_failures, opened_at,
              retry_after, last_category, last_message, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(adapter, hostname) DO UPDATE SET
              status = excluded.status,
              consecutive_failures = excluded.consecutive_failures,
              opened_at = excluded.opened_at,
              retry_after = excluded.retry_after,
              last_category = excluded.last_category,
              last_message = excluded.last_message,
              updated_at = excluded.updated_at
            """,
            (
                adapter,
                hostname,
                circuit_status,
                failures,
                opened_at,
                retry_after,
                classification["category"],
                browser_diagnostics.sanitize_text(message),
                _iso(current),
            ),
        )
    return {
        **classification,
        "circuit": get_circuit(adapter, hostname),
    }


def retry_decision(
    task: dict[str, Any],
    *,
    message: str,
    checkpoint_kind: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    policy = recovery_policy()
    classification = browser_diagnostics.classify_outcome(
        "failed",
        checkpoint_kind,
        message,
    )
    category = str(classification["category"])
    base = {
        "should_retry": False,
        "category": category,
        "next_attempt_at": "",
        "delay_seconds": 0,
        "reason": "",
        "exhausted": False,
    }
    if task.get("submit_started_at") or checkpoint_kind == "submission_uncertain":
        return {**base, "reason": "submission_uncertain"}
    if not bool(policy["enabled"]):
        return {**base, "reason": "policy_disabled"}
    if category not in AUTO_RETRY_CATEGORIES:
        return {**base, "reason": "manual_review_required"}
    attempt_count = int(task.get("attempt_count") or 0)
    if attempt_count >= int(policy["max_attempts"]):
        return {**base, "reason": "attempt_limit", "exhausted": True}
    circuit = get_circuit(str(task.get("adapter") or ""), task_hostname(task))
    if circuit["effective_status"] in {"open", "half_open"}:
        return {
            **base,
            "reason": "circuit_open",
            "exhausted": True,
            "next_attempt_at": str(circuit.get("retry_after") or ""),
        }
    delay = int(policy["base_delay_seconds"]) * (2 ** max(0, attempt_count - 1))
    delay = min(delay, int(policy["max_delay_seconds"]))
    next_attempt = _utc_now(now) + timedelta(seconds=delay)
    return {
        **base,
        "should_retry": True,
        "next_attempt_at": _iso(next_attempt),
        "delay_seconds": delay,
        "reason": "recoverable_failure",
    }


def task_recovery_state(task: dict[str, Any]) -> dict[str, object]:
    circuit = get_circuit(str(task.get("adapter") or ""), task_hostname(task))
    return {
        "category": str(task.get("retry_category") or ""),
        "reason": str(task.get("retry_reason") or ""),
        "next_attempt_at": str(task.get("next_attempt_at") or ""),
        "exhausted": bool(task.get("retry_exhausted")),
        "circuit": circuit,
    }


def list_circuits() -> list[dict[str, Any]]:
    return [
        _circuit_dict(item)
        for item in rows(
            "SELECT * FROM adapter_circuit_breakers ORDER BY updated_at DESC"
        )
    ]


def reset_circuit(adapter: str, hostname: str) -> dict[str, object]:
    circuit = row(
        """
        SELECT * FROM adapter_circuit_breakers
        WHERE adapter = ? AND hostname = ?
        """,
        (adapter, hostname),
    )
    if not circuit:
        raise ValueError("ATS circuit does not exist")
    now = now_iso()
    requeued: list[int] = []
    with connect() as conn:
        conn.execute(
            """
            UPDATE adapter_circuit_breakers
            SET status = 'closed', consecutive_failures = 0, opened_at = '',
                retry_after = '', last_message = ?, updated_at = ?
            WHERE adapter = ? AND hostname = ?
            """,
            ("Circuit reset manually from the local dashboard.", now, adapter, hostname),
        )
        waiting = conn.execute(
            """
            SELECT id, target_url, resume_url FROM application_tasks
            WHERE adapter = ? AND status = 'retry_wait'
              AND retry_reason IN ('circuit_open', 'probe_in_progress')
            """,
            (adapter,),
        ).fetchall()
        for task in waiting:
            target = str(task["resume_url"] or task["target_url"] or "")
            if (urlparse(target).hostname or "").casefold() != hostname.casefold():
                continue
            task_id = int(task["id"])
            requeued.append(task_id)
            conn.execute(
                """
                UPDATE application_tasks
                SET status = 'queued', current_step = 'queued', next_attempt_at = '',
                    retry_reason = '', retry_exhausted = 0, message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("ATS circuit reset. Browser application re-queued.", now, task_id),
            )
            conn.execute(
                """
                INSERT INTO application_task_events(
                  task_id, level, step, message, meta, created_at
                )
                VALUES(?, 'warning', 'queued', ?, ?, ?)
                """,
                (
                    task_id,
                    "ATS circuit reset. Browser application re-queued.",
                    json.dumps({"adapter": adapter, "hostname": hostname}),
                    now,
                ),
            )
    log(
        f"Reset the {adapter} recovery circuit for {hostname}.",
        "warning",
        {"adapter": adapter, "hostname": hostname, "requeued_tasks": requeued},
    )
    return {
        "ok": True,
        "message": f"Reset the {adapter} circuit for {hostname}.",
        "requeued_task_ids": requeued,
        "circuit": get_circuit(adapter, hostname),
    }


def recover_interrupted_circuits() -> int:
    policy = recovery_policy()
    current = _utc_now()
    retry_after = _iso(
        current + timedelta(minutes=int(policy["circuit_cooldown_minutes"]))
    )
    with connect() as conn:
        recovered = conn.execute(
            """
            UPDATE adapter_circuit_breakers
            SET status = 'open', opened_at = ?, retry_after = ?,
                last_message = ?, updated_at = ?
            WHERE status = 'half_open'
            """,
            (
                _iso(current),
                retry_after,
                "The local service stopped during a circuit recovery probe.",
                _iso(current),
            ),
        ).rowcount
    if recovered:
        log(
            f"Reopened {recovered} interrupted ATS recovery circuit(s).",
            "warning",
        )
    return int(recovered)


def dashboard_state() -> dict[str, object]:
    circuits = list_circuits()
    task_items = rows(
        """
        SELECT application_tasks.id, application_tasks.application_id,
               application_tasks.adapter, application_tasks.status,
               application_tasks.attempt_count, application_tasks.next_attempt_at,
               application_tasks.retry_category, application_tasks.retry_reason,
               application_tasks.retry_exhausted, application_tasks.message,
               application_tasks.target_url, application_tasks.resume_url,
               jobs.title, jobs.company
        FROM application_tasks
        JOIN applications ON applications.id = application_tasks.application_id
        JOIN jobs ON jobs.id = applications.job_id
        WHERE application_tasks.status = 'retry_wait'
           OR (application_tasks.status = 'failed'
               AND application_tasks.retry_exhausted = 1)
        ORDER BY application_tasks.updated_at DESC
        LIMIT 50
        """
    )
    for item in task_items:
        item["hostname"] = task_hostname(item)
        item["circuit"] = get_circuit(str(item.get("adapter") or ""), item["hostname"])
        item.pop("target_url", None)
        item.pop("resume_url", None)
    waiting = sum(1 for item in task_items if item["status"] == "retry_wait")
    return {
        "policy": recovery_policy(),
        "summary": {
            "waiting": waiting,
            "exhausted": sum(1 for item in task_items if item["retry_exhausted"]),
            "open_circuits": sum(
                1
                for circuit in circuits
                if circuit["effective_status"] in {"open", "half_open", "probe_ready"}
            ),
        },
        "tasks": task_items,
        "circuits": circuits,
    }
