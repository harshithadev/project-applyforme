from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import GENERATED_DIR
from .db import connect, log, now_iso, row, rows, setting


@dataclass(frozen=True)
class AdapterDefinition:
    name: str
    version: str
    host_suffixes: tuple[str, ...]
    source_aliases: tuple[str, ...]
    apply_selectors: tuple[str, ...]
    apply_labels: str
    manual_path_selectors: tuple[str, ...] = ()
    manual_path_labels: str = ""
    submit_selectors: tuple[str, ...] = ()
    submit_labels: str = r"submit|submit application|send application"
    continue_selectors: tuple[str, ...] = ()
    continue_labels: str = ""
    capabilities: tuple[str, ...] = (
        "application-form",
        "resume-upload",
        "guarded-submit",
    )


ADAPTERS: dict[str, AdapterDefinition] = {
    "greenhouse": AdapterDefinition(
        name="greenhouse",
        version="2026.07.1",
        host_suffixes=("greenhouse.io",),
        source_aliases=("greenhouse",),
        apply_selectors=("a[href*='#app']",),
        apply_labels=r"apply for this job|apply now|apply",
        submit_selectors=("input[type='submit'][value*='Submit' i]",),
    ),
    "lever": AdapterDefinition(
        name="lever",
        version="2026.07.1",
        host_suffixes=("lever.co",),
        source_aliases=("lever",),
        apply_selectors=("a.postings-btn", "a[href$='/apply']", "a[href*='/apply?']"),
        apply_labels=r"apply for this job|apply now|apply",
        submit_selectors=("input[type='submit'][value*='Submit' i]",),
    ),
    "ashby": AdapterDefinition(
        name="ashby",
        version="2026.07.1",
        host_suffixes=("ashbyhq.com",),
        source_aliases=("ashby",),
        apply_selectors=("a[href$='/apply']", "a[href*='/apply?']"),
        apply_labels=r"apply for this job|apply now|apply",
        submit_selectors=("input[type='submit'][value*='Submit' i]",),
        continue_labels=r"next|continue",
        capabilities=(
            "application-form",
            "multi-step",
            "resume-upload",
            "guarded-submit",
        ),
    ),
    "smartrecruiters": AdapterDefinition(
        name="smartrecruiters",
        version="2026.07.1",
        host_suffixes=("smartrecruiters.com", "smrtr.io"),
        source_aliases=("smartrecruiters",),
        apply_selectors=(),
        apply_labels=r"i['’]m interested|apply now|apply",
        submit_selectors=("input[type='submit'][value*='Submit' i]",),
        continue_labels=r"next|continue|save and continue",
        capabilities=(
            "application-form",
            "multi-step",
            "resume-upload",
            "guarded-submit",
        ),
    ),
    "workday": AdapterDefinition(
        name="workday",
        version="2026.07.1",
        host_suffixes=("myworkdayjobs.com", "myworkdaysite.com"),
        source_aliases=("workday",),
        apply_selectors=("[data-automation-id='jobPostingApplyButton']",),
        apply_labels=r"apply now|apply",
        manual_path_selectors=("[data-automation-id='applyManually']",),
        manual_path_labels=r"apply manually",
        submit_selectors=(
            "[data-automation-id='submitButton']",
            "input[type='submit'][value*='Submit' i]",
        ),
        continue_selectors=(
            "[data-automation-id='bottom-navigation-next-button']",
        ),
        continue_labels=r"next|save and continue|save & continue",
        capabilities=(
            "application-form",
            "persistent-login",
            "manual-application-path",
            "multi-step",
            "resume-upload",
            "guarded-submit",
        ),
    ),
}

DRIFT_CATEGORIES = frozenset({"unsupported_form", "submit_control", "step_limit"})
COMPATIBLE_CATEGORIES = frozenset(
    {"submitted", "final_review", "unknown_field", "sensitive_question"}
)
REPLAY_CATEGORIES = DRIFT_CATEGORIES | COMPATIBLE_CATEGORIES


def supported_adapters() -> frozenset[str]:
    return frozenset(ADAPTERS)


def definition(adapter: str) -> AdapterDefinition | None:
    return ADAPTERS.get(str(adapter or "").casefold())


def _host_matches(hostname: str, suffix: str) -> bool:
    return hostname == suffix or hostname.endswith(f".{suffix}")


def detect_adapter(target_url: str, source: str = "", allow_override: bool = True) -> str:
    parsed = urlparse(target_url)
    hostname = (parsed.hostname or "").casefold()
    requested = parse_qs(parsed.query).get("ats", [""])[0].casefold()
    source_name = source.casefold().strip()
    if allow_override and requested in ADAPTERS:
        return requested
    for name, adapter in ADAPTERS.items():
        if any(_host_matches(hostname, suffix) for suffix in adapter.host_suffixes):
            return name
        if source_name in adapter.source_aliases:
            return name
    return "unsupported"


def source_kind(target_url: str) -> str:
    detected = detect_adapter(target_url, allow_override=False)
    return detected if detected in ADAPTERS else "career-page"


def registry_policy() -> dict[str, int]:
    try:
        configured = int(setting("browser_adapter_drift_threshold", "2") or "2")
    except ValueError:
        configured = 2
    return {"drift_threshold": max(1, min(configured, 10))}


def _safe_signature(snapshot: dict[str, object]) -> dict[str, object]:
    controls = [
        {
            "tag": str(item.get("tag") or "")[:30],
            "type": str(item.get("type") or "")[:40],
            "name": str(item.get("name") or "")[:100],
            "question": str(item.get("question") or "")[:160],
            "required": bool(item.get("required")),
        }
        for item in list(snapshot.get("controls") or [])
        if isinstance(item, dict)
    ][:60]
    buttons = [
        {
            "tag": str(item.get("tag") or "")[:30],
            "type": str(item.get("type") or "")[:40],
            "name": str(item.get("name") or "")[:100],
            "question": str(item.get("question") or "")[:160],
            "disabled": bool(item.get("disabled")),
        }
        for item in list(snapshot.get("buttons") or [])
        if isinstance(item, dict)
    ][:30]
    structure = {
        "form_count": max(0, min(int(snapshot.get("form_count") or 0), 100)),
        "controls": controls,
        "buttons": buttons,
    }
    encoded = json.dumps(structure, sort_keys=True, separators=(",", ":")).encode()
    return {
        **structure,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def inspect_snapshot(adapter: str, snapshot: dict[str, object]) -> dict[str, object]:
    spec = definition(adapter)
    signature = _safe_signature(snapshot)
    controls = list(signature["controls"])
    buttons = list(signature["buttons"])
    control_text = " ".join(
        f"{item.get('type', '')} {item.get('name', '')} {item.get('question', '')}"
        for item in controls
        if isinstance(item, dict)
    ).casefold()
    button_text = " ".join(
        str(item.get("question") or "")
        for item in buttons
        if isinstance(item, dict) and not item.get("disabled")
    )
    has_form_surface = bool(
        controls
        and (
            int(signature["form_count"]) > 0
            or "email" in control_text
            or "resume" in control_text
            or "file" in control_text
        )
    )
    progression_pattern = "|".join(
        value
        for value in (
            spec.submit_labels if spec else "",
            spec.continue_labels if spec else "",
        )
        if value
    )
    has_progression_control = bool(
        progression_pattern
        and re.search(
            rf"^\s*(?:{progression_pattern})\s*$",
            button_text,
            re.IGNORECASE,
        )
    )
    if not has_progression_control and progression_pattern:
        has_progression_control = any(
            re.search(
                rf"^\s*(?:{progression_pattern})\s*$",
                str(item.get("question") or ""),
                re.IGNORECASE,
            )
            for item in buttons
            if isinstance(item, dict) and not item.get("disabled")
        )
    return {
        "has_form_surface": has_form_surface,
        "has_progression_control": has_progression_control,
        "signature": signature,
    }


def replay_check(payload: dict[str, object]) -> dict[str, object]:
    adapter = str(payload.get("adapter") or "")
    category = str(payload.get("category") or "")
    snapshot = payload.get("snapshot")
    inspection = inspect_snapshot(
        adapter,
        snapshot if isinstance(snapshot, dict) else {},
    )
    if category == "unsupported_form":
        reproduced = not inspection["has_form_surface"]
    elif category == "submit_control":
        reproduced = bool(
            inspection["has_form_surface"] and not inspection["has_progression_control"]
        )
    elif category == "step_limit":
        reproduced = bool(inspection["has_form_surface"])
    elif category in COMPATIBLE_CATEGORIES:
        reproduced = bool(inspection["has_form_surface"])
    else:
        reproduced = False
    return {
        **inspection,
        "category": category,
        "reproduced": reproduced,
    }


def _state_dict(item: dict[str, Any]) -> dict[str, Any]:
    try:
        item["last_signature"] = json.loads(str(item.get("last_signature_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        item["last_signature"] = {}
    item.pop("last_signature_json", None)
    current = definition(str(item.get("adapter") or ""))
    item["current_version"] = current.version if current else ""
    item["version_current"] = bool(
        current and str(item.get("adapter_version") or "") == current.version
    )
    if item.get("status") == "quarantined" and not item["version_current"]:
        item["effective_status"] = "version_updated"
    else:
        item["effective_status"] = str(item.get("status") or "active")
    return item


def get_host_state(adapter: str, hostname: str) -> dict[str, Any] | None:
    found = row(
        "SELECT * FROM ats_adapter_states WHERE adapter = ? AND hostname = ?",
        (adapter, hostname.casefold()),
    )
    return _state_dict(found) if found else None


def attempt_gate(task: dict[str, Any]) -> dict[str, object]:
    adapter = str(task.get("adapter") or "")
    target_url = str(task.get("resume_url") or task.get("target_url") or "")
    hostname = (urlparse(target_url).hostname or "").casefold()
    spec = definition(adapter)
    state = get_host_state(adapter, hostname)
    if (
        spec
        and state
        and state["effective_status"] == "quarantined"
    ):
        return {
            "allowed": False,
            "reason": "adapter_quarantined",
            "message": (
                f"The {adapter} adapter is quarantined for {hostname} after "
                f"{state['consecutive_drift']} consecutive compatibility failures. "
                "Review a sanitized replay and reactivate it explicitly."
            ),
            "adapter": adapter,
            "hostname": hostname,
            "adapter_version": spec.version,
            "state": state,
        }
    return {
        "allowed": True,
        "reason": "version_updated" if state and state["effective_status"] == "version_updated" else "",
        "adapter": adapter,
        "hostname": hostname,
        "adapter_version": spec.version if spec else "",
        "state": state,
    }


def _create_replay(
    *,
    task: dict[str, Any],
    diagnostic_id: int,
    adapter: str,
    hostname: str,
    adapter_version: str,
    category: str,
    snapshot: dict[str, object],
    signature: dict[str, object],
) -> int:
    existing = row(
        "SELECT id FROM ats_replay_fixtures WHERE diagnostic_id = ?",
        (diagnostic_id,),
    )
    if existing:
        return int(existing["id"])
    now = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO ats_replay_fixtures(
              task_id, diagnostic_id, adapter, hostname, adapter_version,
              category, signature_json, artifact_path, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, '', ?)
            """,
            (
                int(task["id"]),
                diagnostic_id,
                adapter,
                hostname,
                adapter_version,
                category,
                json.dumps(signature, sort_keys=True),
                now,
            ),
        )
        replay_id = int(cursor.lastrowid)
    replay_dir = (GENERATED_DIR / "browser" / "replays").resolve()
    replay_dir.mkdir(parents=True, exist_ok=True)
    artifact = replay_dir / f"replay-{replay_id}.json"
    payload = {
        "schema_version": 1,
        "id": replay_id,
        "diagnostic_id": diagnostic_id,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "hostname": hostname,
        "category": category,
        "expected": "drift" if category in DRIFT_CATEGORIES else "compatible",
        "signature": signature,
        "snapshot": snapshot,
        "created_at": now,
    }
    artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    with connect() as conn:
        conn.execute(
            "UPDATE ats_replay_fixtures SET artifact_path = ? WHERE id = ?",
            (str(artifact), replay_id),
        )
    return replay_id


def record_outcome(
    task: dict[str, Any],
    *,
    diagnostic_id: int,
    category: str,
    message: str,
    snapshot: dict[str, object],
) -> dict[str, Any]:
    adapter = str(task.get("adapter") or "")
    spec = definition(adapter)
    target_url = str(task.get("resume_url") or task.get("target_url") or "")
    hostname = (urlparse(target_url).hostname or "").casefold()
    if not spec or not hostname:
        return {}
    inspection = inspect_snapshot(adapter, snapshot)
    signature = dict(inspection["signature"])
    replay_id = (
        _create_replay(
            task=task,
            diagnostic_id=diagnostic_id,
            adapter=adapter,
            hostname=hostname,
            adapter_version=spec.version,
            category=category,
            snapshot=snapshot,
            signature=signature,
        )
        if category in REPLAY_CATEGORIES
        else 0
    )
    if category not in DRIFT_CATEGORIES | COMPATIBLE_CATEGORIES:
        return {"replay_id": replay_id, "state": get_host_state(adapter, hostname)}

    existing = row(
        "SELECT * FROM ats_adapter_states WHERE adapter = ? AND hostname = ?",
        (adapter, hostname),
    )
    same_version = bool(
        existing and str(existing.get("adapter_version") or "") == spec.version
    )
    consecutive = int(existing.get("consecutive_drift") or 0) if same_version else 0
    total = int(existing.get("total_drift") or 0) if existing else 0
    status = "active"
    quarantined_at = ""
    if category in DRIFT_CATEGORIES:
        consecutive += 1
        total += 1
        if consecutive >= registry_policy()["drift_threshold"]:
            status = "quarantined"
            quarantined_at = now_iso()
    else:
        consecutive = 0
    current = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ats_adapter_states(
              adapter, hostname, adapter_version, status, consecutive_drift,
              total_drift, last_category, last_message, last_signature_json,
              last_diagnostic_id, quarantined_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(adapter, hostname) DO UPDATE SET
              adapter_version = excluded.adapter_version,
              status = excluded.status,
              consecutive_drift = excluded.consecutive_drift,
              total_drift = excluded.total_drift,
              last_category = excluded.last_category,
              last_message = excluded.last_message,
              last_signature_json = excluded.last_signature_json,
              last_diagnostic_id = excluded.last_diagnostic_id,
              quarantined_at = excluded.quarantined_at,
              updated_at = excluded.updated_at
            """,
            (
                adapter,
                hostname,
                spec.version,
                status,
                consecutive,
                total,
                category,
                str(message or "")[:300],
                json.dumps(signature, sort_keys=True),
                diagnostic_id,
                quarantined_at,
                current,
            ),
        )
    if status == "quarantined":
        log(
            f"Quarantined the {adapter} adapter for {hostname} after repeated compatibility drift.",
            "warning",
            {
                "adapter": adapter,
                "hostname": hostname,
                "adapter_version": spec.version,
                "replay_id": replay_id,
            },
        )
    return {"replay_id": replay_id, "state": get_host_state(adapter, hostname)}


def reactivate(adapter: str, hostname: str) -> dict[str, object]:
    spec = definition(adapter)
    normalized_host = hostname.casefold().strip()
    existing = get_host_state(adapter, normalized_host)
    if not spec or not existing:
        raise ValueError("ATS adapter host state does not exist")
    current = now_iso()
    requeued: list[int] = []
    with connect() as conn:
        conn.execute(
            """
            UPDATE ats_adapter_states
            SET adapter_version = ?, status = 'active', consecutive_drift = 0,
                quarantined_at = '', last_message = ?, updated_at = ?
            WHERE adapter = ? AND hostname = ?
            """,
            (
                spec.version,
                "Adapter reactivated explicitly from the local dashboard.",
                current,
                adapter,
                normalized_host,
            ),
        )
        waiting = conn.execute(
            """
            SELECT id, target_url, resume_url FROM application_tasks
            WHERE adapter = ? AND status = 'checkpoint'
              AND checkpoint_kind = 'adapter_quarantined'
            """,
            (adapter,),
        ).fetchall()
        for task in waiting:
            target = str(task["resume_url"] or task["target_url"] or "")
            if (urlparse(target).hostname or "").casefold() != normalized_host:
                continue
            task_id = int(task["id"])
            requeued.append(task_id)
            conn.execute(
                """
                UPDATE application_tasks
                SET status = 'queued', current_step = 'queued', checkpoint_kind = '',
                    checkpoint_json = '{}', message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("ATS adapter reactivated. Browser application re-queued.", current, task_id),
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
                    "ATS adapter reactivated. Browser application re-queued.",
                    json.dumps(
                        {
                            "adapter": adapter,
                            "hostname": normalized_host,
                            "adapter_version": spec.version,
                        }
                    ),
                    current,
                ),
            )
    log(
        f"Reactivated the {adapter} adapter for {normalized_host}.",
        "warning",
        {"adapter": adapter, "hostname": normalized_host, "requeued_tasks": requeued},
    )
    return {
        "ok": True,
        "message": f"Reactivated {adapter} {spec.version} for {normalized_host}.",
        "requeued_task_ids": requeued,
        "state": get_host_state(adapter, normalized_host),
    }


def _replay_dict(item: dict[str, Any]) -> dict[str, Any]:
    try:
        item["signature"] = json.loads(str(item.get("signature_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        item["signature"] = {}
    item["download_available"] = bool(
        item.get("artifact_path") and Path(str(item["artifact_path"])).is_file()
    )
    item.pop("signature_json", None)
    item.pop("artifact_path", None)
    return item


def replay_artifact(replay_id: int) -> dict[str, Any] | None:
    return row(
        "SELECT id, artifact_path FROM ats_replay_fixtures WHERE id = ?",
        (replay_id,),
    )


def list_replays(limit: int = 30) -> list[dict[str, Any]]:
    return [
        _replay_dict(item)
        for item in rows(
            "SELECT * FROM ats_replay_fixtures ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    ]


def dashboard_state() -> dict[str, object]:
    states = [_state_dict(item) for item in rows(
        "SELECT * FROM ats_adapter_states ORDER BY updated_at DESC"
    )]
    by_adapter: dict[str, list[dict[str, Any]]] = {name: [] for name in ADAPTERS}
    for state in states:
        by_adapter.setdefault(str(state["adapter"]), []).append(state)
    registry = []
    for name, spec in ADAPTERS.items():
        public = asdict(spec)
        public.pop("host_suffixes", None)
        public.pop("source_aliases", None)
        public.pop("apply_selectors", None)
        public.pop("manual_path_selectors", None)
        public.pop("submit_selectors", None)
        public.pop("continue_selectors", None)
        public.pop("apply_labels", None)
        public.pop("manual_path_labels", None)
        public.pop("submit_labels", None)
        public.pop("continue_labels", None)
        public["hosts"] = by_adapter.get(name, [])
        registry.append(public)
    replays = list_replays()
    return {
        "policy": registry_policy(),
        "summary": {
            "adapters": len(ADAPTERS),
            "tracked_hosts": len(states),
            "quarantined": sum(
                1 for item in states if item["effective_status"] == "quarantined"
            ),
            "replays": len(rows("SELECT id FROM ats_replay_fixtures")),
        },
        "adapters": registry,
        "recent_replays": replays,
    }
