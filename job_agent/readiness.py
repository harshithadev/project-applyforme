from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from . import automation, documents, emailer, service, writing
from .db import all_settings, connect, log, now_iso, row, rows, set_setting
from .document_readers import ocr_status
from .latex import available_latex_engine


CapabilityOverrides = dict[str, object]


def _enabled(value: object) -> bool:
    return str(value or "").casefold() == "true"


def _values(value: object) -> list[str]:
    return [
        item.strip()
        for item in str(value or "").replace("\n", ",").split(",")
        if item.strip()
    ]


def _bounded_integer(
    value: object,
    minimum: int,
    maximum: int,
) -> int | None:
    try:
        parsed = int(str(value))
    except ValueError:
        return None
    return parsed if minimum <= parsed <= maximum else None


def _check(
    check_id: str,
    title: str,
    status: str,
    message: str,
    *,
    required: bool,
    view: str,
    detail: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": check_id,
        "title": title,
        "status": status,
        "message": message,
        "required": required,
        "view": view,
        "detail": detail or {},
    }


def _capability(
    overrides: CapabilityOverrides,
    key: str,
    provider: Callable[[], object],
) -> object:
    return overrides[key] if key in overrides else provider()


def evaluate_readiness(
    overrides: CapabilityOverrides | None = None,
) -> dict[str, object]:
    supplied = overrides or {}
    settings = all_settings()
    service_state = _capability(supplied, "service", service.status)
    codex = _capability(supplied, "codex", writing.codex_status)
    latex_engine = _capability(
        supplied,
        "latex_engine",
        available_latex_engine,
    )
    ocr = _capability(supplied, "ocr", ocr_status)
    browser_ready = bool(
        _capability(supplied, "browser", automation.playwright_available)
    )
    email = _capability(supplied, "email", emailer.email_status)
    watcher = _capability(supplied, "document_watcher", documents.watcher_status)

    service_value = service_state if isinstance(service_state, dict) else {}
    codex_value = codex if isinstance(codex, dict) else {}
    ocr_value = ocr if isinstance(ocr, dict) else {}
    email_value = email if isinstance(email, dict) else {}
    watcher_value = watcher if isinstance(watcher, dict) else {}

    document_counts = {
        str(item["ingest_status"]): int(item["count"])
        for item in rows(
            "SELECT ingest_status, COUNT(*) AS count FROM documents GROUP BY ingest_status"
        )
    }
    ready_documents = document_counts.get("ready", 0)
    pending_documents = document_counts.get("pending_review", 0)
    document_errors = document_counts.get("error", 0) + document_counts.get(
        "duplicate", 0
    )
    candidate = row("SELECT profile_json FROM candidate_profiles WHERE id = 1")
    try:
        candidate_profile = json.loads(str(candidate["profile_json"])) if candidate else {}
    except json.JSONDecodeError:
        candidate_profile = {}
    if not isinstance(candidate_profile, dict):
        candidate_profile = {}
    contact = (
        candidate_profile.get("contact", {})
    )
    identity_ready = bool(
        candidate_profile.get("name")
        and isinstance(contact, dict)
        and (contact.get("emails") or contact.get("phones"))
    )

    career_urls = _values(settings.get("career_urls"))
    discovery_providers = _values(settings.get("discovery_providers"))
    role_keywords = _values(settings.get("role_keywords"))
    role_families = _values(settings.get("target_role_families"))
    additional_title_aliases = _values(settings.get("additional_title_aliases"))
    locations = _values(settings.get("locations"))
    career_stage_mode = str(
        settings.get("career_stage_mode", "open") or "open"
    ).casefold()
    if career_stage_mode == "graduate":
        role_targets = role_families or additional_title_aliases
        targeting_message = (
            f"{len(role_families)} management role family(s), "
            f"{len(additional_title_aliases)} additional title(s), and "
            f"{len(locations)} optional location preference(s) are configured."
            if role_targets
            else (
                "Select a management role family or add an accepted title."
            )
        )
    else:
        role_targets = role_keywords
        targeting_message = (
            f"{len(role_keywords)} role keyword(s) and "
            f"{len(locations)} optional location preference(s) are configured."
            if role_keywords
            else "Add at least one role keyword."
        )
    targeting_ready = bool(role_targets)
    job_count = int((row("SELECT COUNT(*) AS count FROM jobs") or {"count": 0})["count"])
    discovery_ready = bool(discovery_providers or career_urls or job_count)
    email_configured = bool(email_value.get("configured"))
    email_verified = (
        settings.get("smtp_verification_status") == "verified"
        and bool(settings.get("smtp_verified_at"))
    )
    email_ready = email_configured and email_verified
    pipeline_enabled = _enabled(settings.get("pipeline_enabled"))
    minimum_score = _bounded_integer(
        settings.get("pipeline_min_score"),
        0,
        100,
    )
    daily_application_limit = _bounded_integer(
        settings.get("daily_application_limit"),
        1,
        200,
    )
    policy_ready = bool(
        settings.get("mode")
        in {"review", "assisted_autonomous", "rules_autonomous"}
        and minimum_score is not None
        and daily_application_limit is not None
    )
    rules_policy_ready = bool(
        settings.get("mode") == "rules_autonomous"
        and pipeline_enabled
        and _enabled(settings.get("pipeline_auto_approve"))
        and _enabled(settings.get("pipeline_auto_apply"))
        and _enabled(settings.get("browser_submit_enabled"))
    )

    checks = [
        _check(
            "service",
            "Background service",
            "pass" if service_value.get("running") else "blocked",
            str(
                service_value.get("message")
                or "The login service must be running for background work."
            ),
            required=True,
            view="settings",
            detail={
                "pid": service_value.get("pid"),
                "installed": service_value.get("installed", False),
            },
        ),
        _check(
            "codex",
            "ChatGPT-authenticated Codex",
            "pass" if codex_value.get("ready") else "blocked",
            str(codex_value.get("message") or "Codex status is unavailable."),
            required=True,
            view="settings",
            detail={"auth": codex_value.get("auth", "")},
        ),
        _check(
            "latex",
            "LaTeX PDF compiler",
            "pass" if latex_engine else "blocked",
            (
                f"{latex_engine} is available for tailored resume compilation."
                if latex_engine
                else "Install a supported LaTeX engine before generating application packages."
            ),
            required=True,
            view="settings",
            detail={"engine": latex_engine or ""},
        ),
        _check(
            "documents",
            "Candidate evidence",
            "pass" if ready_documents else "blocked",
            (
                f"{ready_documents} approved source document(s) are available."
                if ready_documents
                else "Add and approve at least one source document."
            ),
            required=True,
            view="documents",
            detail={
                "ready": ready_documents,
                "pending_review": pending_documents,
                "errors": document_errors,
            },
        ),
        _check(
            "identity",
            "Profile identity",
            "pass" if identity_ready else "warning",
            (
                "Candidate name and contact details were found in approved evidence."
                if identity_ready
                else "Add a name and email or phone number to an approved source document."
            ),
            required=False,
            view="documents",
        ),
        _check(
            "discovery",
            "Job discovery input",
            "pass" if discovery_ready else "blocked",
            (
                f"{len(discovery_providers)} broad provider(s), "
                f"{len(career_urls)} company source(s), and "
                f"{job_count} saved job(s) are available."
                if discovery_ready
                else "Enable a discovery provider, add a company URL, or save a job manually."
            ),
            required=True,
            view="jobs",
            detail={
                "discovery_providers": len(discovery_providers),
                "career_sources": len(career_urls),
                "saved_jobs": job_count,
            },
        ),
        _check(
            "targeting",
            "Search targeting",
            "pass" if targeting_ready else "blocked",
            targeting_message,
            required=True,
            view="settings",
            detail={
                "career_stage_mode": career_stage_mode,
                "role_families": len(role_families),
                "additional_title_aliases": len(additional_title_aliases),
                "role_keywords": len(role_keywords),
                "locations": len(locations),
            },
        ),
        _check(
            "policy",
            "Application limits and mode",
            "pass" if policy_ready else "blocked",
            (
                f"{settings.get('mode', 'review').replace('_', ' ')} mode with score "
                f"{minimum_score} and a daily limit of {daily_application_limit}."
                if policy_ready
                else "Choose a valid mode, minimum score, and daily application limit."
            ),
            required=True,
            view="setup",
        ),
        _check(
            "browser",
            "Browser automation",
            "pass" if browser_ready else "blocked",
            (
                "Playwright is available for guarded application workflows."
                if browser_ready
                else "Install the Playwright browser before queueing applications."
            ),
            required=True,
            view="settings",
        ),
        _check(
            "watcher",
            "Document folder watcher",
            "pass" if watcher_value.get("running") else "warning",
            (
                "The document folder watcher is running."
                if watcher_value.get("running")
                else "The watcher starts with the persistent local service."
            ),
            required=False,
            view="documents",
        ),
        _check(
            "ocr",
            "Scanned-document OCR",
            "pass" if ocr_value.get("available") else "warning",
            str(ocr_value.get("message") or "OCR status is unavailable."),
            required=False,
            view="documents",
        ),
        _check(
            "email",
            "Outreach email",
            "pass" if email_ready else "warning",
            (
                "SMTP credentials were verified without sending an email."
                if email_ready
                else (
                    "SMTP is configured but has not passed connection verification."
                    if email_configured
                    else str(
                        email_value.get("message")
                        or "Email delivery is optional and not configured."
                    )
                )
            ),
            required=False,
            view="outreach",
            detail={
                "configured": email_configured,
                "verified_at": settings.get("smtp_verified_at", ""),
            },
        ),
        _check(
            "pipeline",
            "Automatic application pipeline",
            "pass" if pipeline_enabled else "warning",
            (
                "The score-gated application pipeline is enabled."
                if pipeline_enabled
                else "The pipeline is disabled; jobs will not advance automatically."
            ),
            required=False,
            view="settings",
        ),
        _check(
            "rules_policy",
            "Rules-autonomous safeguards",
            "pass" if rules_policy_ready else "warning",
            (
                "All independent rules-autonomous approval and submission switches are enabled."
                if rules_policy_ready
                else "Rules-autonomous operation remains unavailable until every explicit safeguard is enabled."
            ),
            required=False,
            view="settings",
        ),
    ]
    by_id = {str(item["id"]): item for item in checks}

    def mode(
        mode_id: str,
        title: str,
        requirement_ids: list[str],
        message: str,
        extra_ready: bool = True,
    ) -> dict[str, object]:
        missing = [
            check_id
            for check_id in requirement_ids
            if by_id[check_id]["status"] != "pass"
        ]
        ready = not missing and extra_ready
        return {
            "id": mode_id,
            "title": title,
            "ready": ready,
            "missing": missing,
            "message": message,
        }

    modes = [
        mode(
            "tailoring",
            "Tailored documents",
            ["codex", "latex", "documents"],
            "Generate evidence-grounded LaTeX resumes and written materials.",
        ),
        mode(
            "review_automation",
            "Review automation",
            [
                "service",
                "codex",
                "latex",
                "documents",
                "discovery",
                "targeting",
                "policy",
                "browser",
            ],
            "Discover, tailor, fill, and pause for explicit review.",
        ),
        mode(
            "outreach",
            "Verified outreach",
            ["codex", "documents", "email"],
            "Prepare and deliver approved hiring-manager outreach.",
        ),
        mode(
            "rules_autonomous",
            "Rules autonomous",
            [
                "service",
                "codex",
                "latex",
                "documents",
                "discovery",
                "targeting",
                "policy",
                "browser",
                "rules_policy",
            ],
            "Run only within explicit scoring, approval, submission, and daily-limit rules.",
            extra_ready=rules_policy_ready,
        ),
    ]
    review_mode = next(item for item in modes if item["id"] == "review_automation")
    tailoring_mode = next(item for item in modes if item["id"] == "tailoring")
    required_ids = [
        "service",
        "codex",
        "latex",
        "documents",
        "discovery",
        "targeting",
        "policy",
        "browser",
    ]
    passed = sum(1 for check_id in required_ids if by_id[check_id]["status"] == "pass")
    score = round(passed / len(required_ids) * 100)
    overall_status = (
        "ready"
        if review_mode["ready"]
        else "attention"
        if tailoring_mode["ready"]
        else "blocked"
    )
    return {
        "status": overall_status,
        "score": score,
        "checks": checks,
        "modes": modes,
        "summary": {
            "required_passed": passed,
            "required_total": len(required_ids),
            "blocking": [
                check_id
                for check_id in required_ids
                if by_id[check_id]["status"] != "pass"
            ],
            "warnings": sum(1 for item in checks if item["status"] == "warning"),
        },
        "setup_completed_at": settings.get("setup_completed_at", ""),
        "evaluated_at": now_iso(),
    }


def _history(limit: int = 20) -> list[dict[str, object]]:
    found = rows(
        """
        SELECT id, status, score, snapshot_json, created_at
        FROM readiness_runs
        ORDER BY id DESC LIMIT ?
        """,
        (max(1, min(limit, 100)),),
    )
    for item in found:
        try:
            snapshot = json.loads(str(item.pop("snapshot_json") or "{}"))
        except json.JSONDecodeError:
            snapshot = {}
        item["blocking"] = snapshot.get("summary", {}).get("blocking", [])
        item["modes"] = snapshot.get("modes", [])
    return found


def readiness_state(
    overrides: CapabilityOverrides | None = None,
) -> dict[str, object]:
    return {
        **evaluate_readiness(overrides),
        "history": _history(),
    }


def run_preflight(
    overrides: CapabilityOverrides | None = None,
) -> dict[str, object]:
    snapshot = evaluate_readiness(overrides)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO readiness_runs(status, score, snapshot_json, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (
                snapshot["status"],
                snapshot["score"],
                json.dumps(snapshot),
                now_iso(),
            ),
        )
        run_id = int(cursor.lastrowid)
    log(
        f"Readiness preflight finished at {snapshot['score']}% with status {snapshot['status']}.",
        meta={
            "readiness_run_id": run_id,
            "blocking": snapshot["summary"]["blocking"],
        },
    )
    return {"run_id": run_id, **snapshot, "history": _history()}


def complete_setup(
    overrides: CapabilityOverrides | None = None,
) -> dict[str, object]:
    snapshot = evaluate_readiness(overrides)
    review_mode = next(
        item for item in snapshot["modes"] if item["id"] == "review_automation"
    )
    if not review_mode["ready"]:
        missing = ", ".join(review_mode["missing"])
        raise ValueError(f"Complete the blocked review-automation checks first: {missing}")
    completed_at = now_iso()
    set_setting("setup_completed_at", completed_at)
    log("Completed ApplyForMe readiness setup.")
    return {**snapshot, "setup_completed_at": completed_at}


def test_codex_connection() -> dict[str, object]:
    result = writing.codex_status(force=True)
    if not result.get("ready"):
        raise ValueError(str(result.get("message") or "Codex is not ready"))
    return {"ok": True, **result}


def test_email_connection(
    verifier: Callable[[], dict[str, str]] = emailer.verify_smtp_connection,
) -> dict[str, object]:
    try:
        result = verifier()
    except Exception as exc:
        set_setting("smtp_verification_status", "failed")
        set_setting("smtp_verification_message", str(exc))
        set_setting("smtp_verified_at", "")
        raise
    verified_at = now_iso()
    set_setting("smtp_verification_status", "verified")
    set_setting("smtp_verification_message", result["message"])
    set_setting("smtp_verified_at", verified_at)
    log("Verified SMTP connection without sending an email.")
    return {"ok": True, **result, "verified_at": verified_at}
