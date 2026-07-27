from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from . import ats_adapters
from .config import GENERATED_DIR
from .db import connect, log, now_iso, row, rows


MAX_TEXT = 300
MAX_CONTROLS = 60
MAX_TELEMETRY = 20
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SECRET_PATTERN = re.compile(
    r"\b(password|passwd|token|secret|authorization|cookie)\s*[:=]\s*"
    r"(?:(?:bearer|basic)\s+)?[^\s,;]+",
    re.IGNORECASE,
)


CLASSIFICATIONS: dict[str, tuple[str, bool, str]] = {
    "captcha": (
        "warning",
        True,
        "Open the manual takeover window, complete the human verification, then resume automation.",
    ),
    "login": (
        "warning",
        True,
        "Open the guided sign-in window. The authenticated local session can then resume this task.",
    ),
    "session_busy": (
        "warning",
        True,
        "Close or complete the active ATS browser window, then retry this task.",
    ),
    "daily_limit": (
        "warning",
        True,
        "Wait for the daily application counter to reset or raise the configured limit intentionally.",
    ),
    "unknown_field": (
        "info",
        True,
        "Provide the missing answer in the checkpoint and continue the application.",
    ),
    "sensitive_question": (
        "info",
        True,
        "Review the sensitive question and explicitly approve an answer before continuing.",
    ),
    "final_review": (
        "info",
        True,
        "Review the completed form and screenshot, then explicitly approve final submission.",
    ),
    "unsupported_site": (
        "warning",
        False,
        "Complete this application manually or add a tested adapter for this ATS before retrying.",
    ),
    "unsupported_form": (
        "warning",
        False,
        "Use manual takeover for this form and review whether the ATS adapter needs an update.",
    ),
    "submit_control": (
        "warning",
        True,
        "Use manual takeover to inspect the missing navigation control, then resume from the captured page.",
    ),
    "step_limit": (
        "warning",
        True,
        "Inspect the remaining steps in manual takeover and resume only after confirming the workflow is bounded.",
    ),
    "submission_uncertain": (
        "critical",
        False,
        "Inspect the ATS directly before taking another action. Do not submit again until status is verified.",
    ),
    "submitted": (
        "success",
        False,
        "No recovery action is required. The ATS confirmation was verified.",
    ),
    "submitted_manually": (
        "success",
        False,
        "No recovery action is required. Manual submission confirmation was verified.",
    ),
    "timeout": (
        "error",
        True,
        "Check the ATS status and network connection, then retry once from the saved session.",
    ),
    "network": (
        "error",
        True,
        "Check local connectivity and ATS availability, then retry from the saved session.",
    ),
    "browser_environment": (
        "error",
        True,
        "Verify the local Playwright browser installation and saved profile, then retry.",
    ),
    "automation_error": (
        "error",
        True,
        "Inspect the diagnostic metadata and screenshot, then retry or switch to manual takeover.",
    ),
}


def sanitize_text(value: object, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = EMAIL_PATTERN.sub("[redacted-email]", text)
    text = PHONE_PATTERN.sub("[redacted-phone]", text)
    text = URL_PATTERN.sub(lambda match: redact_url(match.group(0)), text)
    text = SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text[:limit]


def redact_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname.casefold()
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port else hostname
        return urlunparse((parsed.scheme, netloc, parsed.path or "/", "", "", ""))
    except Exception:
        return ""


def _safe_int(value: object, maximum: int = 10_000) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError):
        return 0


def _sanitize_control(control: object) -> dict[str, object] | None:
    if not isinstance(control, dict):
        return None
    return {
        "question": sanitize_text(control.get("question") or control.get("label"), 160),
        "tag": sanitize_text(control.get("tag"), 30),
        "type": sanitize_text(control.get("type"), 40),
        "name": sanitize_text(control.get("name"), 100),
        "required": bool(control.get("required")),
        "filled": bool(control.get("filled")),
        "disabled": bool(control.get("disabled")),
        "options": [
            sanitize_text(option, 100)
            for option in list(control.get("options") or [])[:20]
            if sanitize_text(option, 100)
        ],
    }


def _sanitize_telemetry(telemetry: object) -> dict[str, list[dict[str, str]]]:
    source = telemetry if isinstance(telemetry, dict) else {}
    console: list[dict[str, str]] = []
    for item in list(source.get("console") or [])[-MAX_TELEMETRY:]:
        if isinstance(item, dict):
            console.append(
                {
                    "type": sanitize_text(item.get("type"), 30),
                    "message": sanitize_text(item.get("message")),
                }
            )
    network: list[dict[str, str]] = []
    for item in list(source.get("network_failures") or [])[-MAX_TELEMETRY:]:
        if isinstance(item, dict):
            network.append(
                {
                    "method": sanitize_text(item.get("method"), 12),
                    "url": redact_url(item.get("url")),
                    "error": sanitize_text(item.get("error")),
                }
            )
    return {"console": console, "network_failures": network}


def attach_telemetry(page: Any) -> dict[str, list[dict[str, str]]]:
    telemetry: dict[str, list[dict[str, str]]] = {
        "console": [],
        "network_failures": [],
    }

    def on_console(message: Any) -> None:
        if len(telemetry["console"]) >= MAX_TELEMETRY:
            return
        telemetry["console"].append(
            {
                "type": str(getattr(message, "type", "") or ""),
                "message": str(getattr(message, "text", "") or ""),
            }
        )

    def on_request_failed(request: Any) -> None:
        if len(telemetry["network_failures"]) >= MAX_TELEMETRY:
            return
        telemetry["network_failures"].append(
            {
                "method": str(getattr(request, "method", "") or ""),
                "url": str(getattr(request, "url", "") or ""),
                "error": str(getattr(request, "failure", "") or ""),
            }
        )

    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)
    return telemetry


def capture_page_snapshot(
    page: Any,
    controls: list[dict[str, Any]] | None = None,
    telemetry: dict[str, list[dict[str, str]]] | None = None,
    screenshot: str = "",
) -> dict[str, object]:
    try:
        title = page.title()
    except Exception:
        title = ""
    frames: list[str] = []
    form_count = 0
    buttons: list[dict[str, object]] = []
    try:
        frame_items = list(page.frames)[:12]
    except Exception:
        frame_items = []
    for frame in frame_items:
        try:
            frame_url = redact_url(frame.url)
            if frame_url and frame_url not in frames:
                frames.append(frame_url)
            form_count += int(frame.locator("form").count())
            raw_buttons = frame.locator(
                "button, input[type='submit'], input[type='button'], "
                "a[role='button'], [data-automation-id*='apply' i]"
            ).evaluate_all(
                """
                nodes => nodes.slice(0, 20).map(node => ({
                  tag: node.tagName.toLowerCase(),
                  type: node.getAttribute("type") || "",
                  name: node.getAttribute("name") || "",
                  label: node.getAttribute("aria-label")
                    || node.innerText
                    || node.getAttribute("title")
                    || "",
                  disabled: Boolean(node.disabled)
                }))
                """
            )
            for item in raw_buttons:
                if len(buttons) >= 30:
                    break
                sanitized = _sanitize_control(item)
                if sanitized:
                    buttons.append(sanitized)
        except Exception:
            continue
    safe_controls = [
        item
        for item in (_sanitize_control(control) for control in (controls or [])[:MAX_CONTROLS])
        if item
    ]
    return {
        "url": redact_url(getattr(page, "url", "")),
        "title": sanitize_text(title, 160),
        "frames": frames,
        "form_count": _safe_int(form_count, 100),
        "controls": safe_controls,
        "buttons": buttons,
        "telemetry": _sanitize_telemetry(telemetry),
        "screenshot": Path(str(screenshot or "")).name,
    }


def classify_outcome(
    status: str,
    checkpoint_kind: str = "",
    message: str = "",
    manual: bool = False,
) -> dict[str, object]:
    if status == "submitted":
        category = "submitted_manually" if manual else "submitted"
    elif checkpoint_kind:
        category = checkpoint_kind
    else:
        lowered = message.casefold()
        if "timeout" in lowered or "timed out" in lowered:
            category = "timeout"
        elif any(term in lowered for term in ("net::", "dns", "connection", "network", "navigation")):
            category = "network"
        elif any(term in lowered for term in ("playwright", "browser", "chromium", "profile", "context")):
            category = "browser_environment"
        else:
            category = "automation_error"
    severity, retryable, recommendation = CLASSIFICATIONS.get(
        category,
        CLASSIFICATIONS["automation_error"],
    )
    return {
        "category": category,
        "severity": severity,
        "retryable": retryable,
        "recommendation": recommendation,
    }


def _safe_snapshot(snapshot: object, target_url: str) -> dict[str, object]:
    source = snapshot if isinstance(snapshot, dict) else {}
    return {
        "url": redact_url(source.get("url") or target_url),
        "title": sanitize_text(source.get("title"), 160),
        "frames": [
            redacted
            for redacted in (redact_url(item) for item in list(source.get("frames") or [])[:12])
            if redacted
        ],
        "form_count": _safe_int(source.get("form_count"), 100),
        "controls": [
            item
            for item in (
                _sanitize_control(control)
                for control in list(source.get("controls") or [])[:MAX_CONTROLS]
            )
            if item
        ],
        "buttons": [
            item
            for item in (
                _sanitize_control(control)
                for control in list(source.get("buttons") or [])[:30]
            )
            if item
        ],
        "telemetry": _sanitize_telemetry(source.get("telemetry")),
        "screenshot": Path(str(source.get("screenshot") or "")).name,
    }


def _update_health(
    conn: Any,
    *,
    adapter: str,
    hostname: str,
    status: str,
    category: str,
    message: str,
    manual: bool,
    count_attempt: bool,
) -> None:
    existing = conn.execute(
        "SELECT * FROM adapter_health WHERE adapter = ? AND hostname = ?",
        (adapter, hostname),
    ).fetchone()
    counts: dict[str, int] = {}
    if existing:
        try:
            counts = {
                str(key): int(value)
                for key, value in json.loads(existing["category_counts_json"] or "{}").items()
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            counts = {}
    counts[category] = counts.get(category, 0) + 1
    now = now_iso()
    attempts = int(existing["attempts"]) if existing else 0
    submitted = int(existing["submitted"]) if existing else 0
    checkpoints = int(existing["checkpoints"]) if existing else 0
    failures = int(existing["failures"]) if existing else 0
    manual_submissions = int(existing["manual_submissions"]) if existing else 0
    attempts += 1 if count_attempt else 0
    submitted += 1 if status == "submitted" else 0
    checkpoints += 1 if status == "checkpoint" else 0
    failures += 1 if status == "failed" else 0
    manual_submissions += 1 if manual and status == "submitted" else 0
    conn.execute(
        """
        INSERT INTO adapter_health(
          adapter, hostname, attempts, submitted, checkpoints, failures,
          manual_submissions, last_outcome, last_category, last_message,
          category_counts_json, last_attempt_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(adapter, hostname) DO UPDATE SET
          attempts = excluded.attempts,
          submitted = excluded.submitted,
          checkpoints = excluded.checkpoints,
          failures = excluded.failures,
          manual_submissions = excluded.manual_submissions,
          last_outcome = excluded.last_outcome,
          last_category = excluded.last_category,
          last_message = excluded.last_message,
          category_counts_json = excluded.category_counts_json,
          last_attempt_at = excluded.last_attempt_at,
          updated_at = excluded.updated_at
        """,
        (
            adapter,
            hostname,
            attempts,
            submitted,
            checkpoints,
            failures,
            manual_submissions,
            status,
            category,
            sanitize_text(message),
            json.dumps(counts, sort_keys=True),
            now if count_attempt else (str(existing["last_attempt_at"]) if existing else ""),
            now,
        ),
    )


def record_outcome(
    task: dict[str, Any],
    *,
    status: str,
    checkpoint_kind: str = "",
    message: str = "",
    snapshot: dict[str, object] | None = None,
    manual: bool = False,
    count_attempt: bool = True,
) -> dict[str, Any]:
    task_id = int(task["id"])
    adapter = sanitize_text(task.get("adapter") or "unsupported", 60)
    target_url = str(task.get("resume_url") or task.get("target_url") or "")
    hostname = (urlparse(target_url).hostname or "").casefold()
    classification = classify_outcome(status, checkpoint_kind, message, manual)
    safe_snapshot = _safe_snapshot(snapshot, target_url)
    created_at = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO browser_diagnostic_bundles(
              task_id, adapter, hostname, outcome_status, category, severity,
              retryable, recommendation, summary_json, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                adapter,
                hostname,
                status,
                classification["category"],
                classification["severity"],
                1 if classification["retryable"] else 0,
                classification["recommendation"],
                json.dumps(safe_snapshot, sort_keys=True),
                created_at,
            ),
        )
        bundle_id = int(cursor.lastrowid)
        _update_health(
            conn,
            adapter=adapter,
            hostname=hostname,
            status=status,
            category=str(classification["category"]),
            message=message,
            manual=manual,
            count_attempt=count_attempt,
        )

    artifact_path = ""
    task_dir = Path(str(task.get("artifact_dir") or "")).resolve()
    expected_root = (GENERATED_DIR / "browser" / "tasks").resolve()
    if expected_root in task_dir.parents:
        diagnostics_dir = task_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        artifact = diagnostics_dir / f"diagnostic-{bundle_id}.json"
        payload = {
            "schema_version": 1,
            "id": bundle_id,
            "task_id": task_id,
            "adapter": adapter,
            "hostname": hostname,
            "outcome_status": status,
            **classification,
            "message": sanitize_text(message),
            "target_url": redact_url(target_url),
            "snapshot": safe_snapshot,
            "created_at": created_at,
        }
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        artifact_path = str(artifact)
        with connect() as conn:
            conn.execute(
                "UPDATE browser_diagnostic_bundles SET artifact_path = ? WHERE id = ?",
                (artifact_path, bundle_id),
            )
    try:
        ats_adapters.record_outcome(
            task,
            diagnostic_id=bundle_id,
            category=str(classification["category"]),
            message=sanitize_text(message),
            snapshot=safe_snapshot,
        )
    except Exception as exc:
        # Diagnostics must remain available even if adapter lifecycle tracking fails.
        log(
            f"ATS adapter lifecycle tracking could not be saved: {sanitize_text(exc)}",
            "warning",
            {"application_task_id": task_id, "diagnostic_id": bundle_id},
        )
    return get_bundle(bundle_id) or {}


def _bundle_dict(bundle: dict[str, Any]) -> dict[str, Any]:
    try:
        bundle["snapshot"] = json.loads(str(bundle.get("summary_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        bundle["snapshot"] = {}
    bundle["retryable"] = bool(bundle.get("retryable"))
    bundle["download_available"] = bool(
        bundle.get("artifact_path") and Path(str(bundle["artifact_path"])).is_file()
    )
    bundle.pop("summary_json", None)
    bundle.pop("artifact_path", None)
    return bundle


def get_bundle(bundle_id: int) -> dict[str, Any] | None:
    bundle = row("SELECT * FROM browser_diagnostic_bundles WHERE id = ?", (bundle_id,))
    return _bundle_dict(bundle) if bundle else None


def bundle_artifact(bundle_id: int) -> dict[str, Any] | None:
    return row(
        "SELECT id, task_id, artifact_path FROM browser_diagnostic_bundles WHERE id = ?",
        (bundle_id,),
    )


def list_for_task(task_id: int, limit: int = 10) -> list[dict[str, Any]]:
    return [
        _bundle_dict(bundle)
        for bundle in rows(
            """
            SELECT * FROM browser_diagnostic_bundles
            WHERE task_id = ? ORDER BY id DESC LIMIT ?
            """,
            (task_id, limit),
        )
    ]


def adapter_health() -> list[dict[str, Any]]:
    health = rows("SELECT * FROM adapter_health ORDER BY updated_at DESC")
    for item in health:
        try:
            item["category_counts"] = json.loads(str(item.get("category_counts_json") or "{}"))
        except (json.JSONDecodeError, TypeError):
            item["category_counts"] = {}
        item.pop("category_counts_json", None)
        attempts = int(item.get("attempts") or 0)
        item["success_rate"] = round((int(item.get("submitted") or 0) / attempts) * 100) if attempts else 0
        if item.get("last_outcome") == "submitted":
            item["status"] = "healthy"
        elif item.get("last_outcome") == "failed":
            item["status"] = "failing"
        else:
            item["status"] = "attention"
    return health


def dashboard_state() -> dict[str, object]:
    total = row("SELECT COUNT(*) AS count FROM browser_diagnostic_bundles")
    retryable = row(
        "SELECT COUNT(*) AS count FROM browser_diagnostic_bundles WHERE retryable = 1"
    )
    critical = row(
        """
        SELECT COUNT(*) AS count FROM browser_diagnostic_bundles
        WHERE severity IN ('error', 'critical')
        """
    )
    return {
        "summary": {
            "bundles": int(total["count"]) if total else 0,
            "retryable": int(retryable["count"]) if retryable else 0,
            "critical": int(critical["count"]) if critical else 0,
        },
        "adapter_health": adapter_health(),
        "recent": [
            _bundle_dict(bundle)
            for bundle in rows(
                "SELECT * FROM browser_diagnostic_bundles ORDER BY id DESC LIMIT 20"
            )
        ],
    }
