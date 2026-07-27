from __future__ import annotations

import importlib.util
import json
import re
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import browser_sessions
from .config import GENERATED_DIR
from .db import connect, log, now_iso, row, rows, setting
from .latex import application_dir
from .profile import structured_profile


RISKY_PATTERNS = re.compile(
    r"salary|compensation|sponsor|visa|authorization|relocat|disabil|gender|race|"
    r"ethnic|veteran|criminal|background|date of birth|age|privacy|consent|"
    r"data processing|terms and conditions",
    re.IGNORECASE,
)
SUCCESS_PATTERNS = re.compile(
    r"thank you|application (?:was )?(?:submitted|received)|we(?:'|’)ve received your application",
    re.IGNORECASE,
)
SUPPORTED_ADAPTERS = frozenset(
    {"greenhouse", "lever", "ashby", "smartrecruiters", "workday"}
)
MAX_FORM_STEPS = 8
_worker_event = threading.Event()
_worker_lock = threading.Lock()
_worker_started = False


def playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except ModuleNotFoundError:
        return False


def submitted_today_count() -> int:
    found = row(
        """
        SELECT COUNT(*) AS count
        FROM applications
        WHERE status = 'submitted'
          AND date(updated_at) = date('now')
        """
    )
    return int(found["count"]) if found else 0


def _json_value(value: object, fallback: object) -> object:
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError):
        return fallback


def _task_dict(task: dict[str, Any]) -> dict[str, Any]:
    for key, fallback in (
        ("checkpoint_json", {}),
        ("answers_json", {}),
        ("form_snapshot_json", []),
        ("result_json", {}),
        ("screenshots_json", []),
    ):
        task[key.removesuffix("_json")] = _json_value(task.get(key), fallback)
    task["events"] = rows(
        "SELECT id, level, step, message, meta, created_at "
        "FROM application_task_events WHERE task_id = ? ORDER BY id",
        (int(task["id"]),),
    )
    for event in task["events"]:
        event["meta"] = _json_value(event.get("meta"), {})
    session_id = int(task.get("browser_session_id") or 0)
    task["browser_session"] = (
        browser_sessions.get_session(session_id) if session_id else None
    )
    return task


def get_task(task_id: int) -> dict[str, Any] | None:
    task = row("SELECT * FROM application_tasks WHERE id = ?", (task_id,))
    return _task_dict(task) if task else None


def list_tasks() -> list[dict[str, Any]]:
    return [
        _task_dict(task)
        for task in rows(
            "SELECT * FROM application_tasks ORDER BY created_at DESC, id DESC LIMIT 100"
        )
    ]


def automation_status() -> dict[str, object]:
    pending = row(
        "SELECT COUNT(*) AS count FROM application_tasks "
        "WHERE status IN ('queued', 'running', 'checkpoint')"
    )
    checkpoints = row(
        "SELECT COUNT(*) AS count FROM application_tasks WHERE status = 'checkpoint'"
    )
    return {
        "playwright_available": playwright_available(),
        "mode": setting("mode", "review"),
        "submitted_today": submitted_today_count(),
        "daily_limit": int(setting("daily_application_limit", "10") or "10"),
        "pending": int(pending["count"]) if pending else 0,
        "checkpoints": int(checkpoints["count"]) if checkpoints else 0,
        "final_submit_enabled": setting("browser_submit_enabled", "false") == "true",
        "supported_adapters": sorted(SUPPORTED_ADAPTERS),
        "sessions": browser_sessions.session_status(),
    }


def _application_record(application_id: int) -> dict[str, Any] | None:
    return row(
        """
        SELECT applications.*, jobs.url, jobs.apply_url, jobs.title, jobs.company,
               jobs.source, jobs.description
        FROM applications
        JOIN jobs ON jobs.id = applications.job_id
        WHERE applications.id = ?
        """,
        (application_id,),
    )


def _adapter_name(target_url: str, source: str = "") -> str:
    parsed = urlparse(target_url)
    hostname = (parsed.hostname or "").casefold()
    requested = parse_qs(parsed.query).get("ats", [""])[0].casefold()
    source_name = source.casefold()
    if requested in SUPPORTED_ADAPTERS:
        return requested
    if "greenhouse.io" in hostname or "greenhouse" in source_name:
        return "greenhouse"
    if "lever.co" in hostname or source_name == "lever":
        return "lever"
    if "ashbyhq.com" in hostname or source_name == "ashby":
        return "ashby"
    if (
        "smartrecruiters.com" in hostname
        or hostname == "smrtr.io"
        or "smartrecruiters" in source_name
    ):
        return "smartrecruiters"
    if (
        "myworkdayjobs.com" in hostname
        or "myworkdaysite.com" in hostname
        or "workday" in source_name
    ):
        return "workday"
    return "unsupported"


def _record_task_event(
    task_id: int,
    step: str,
    message: str,
    level: str = "info",
    meta: dict[str, object] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO application_task_events(task_id, level, step, message, meta, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (task_id, level, step, message, json.dumps(meta or {}), now_iso()),
        )
        conn.execute(
            "UPDATE application_tasks SET current_step = ?, message = ?, updated_at = ? WHERE id = ?",
            (step, message, now_iso(), task_id),
        )
    log(message, level, {"application_task_id": task_id, "step": step, **(meta or {})})


def apply_application(application_id: int) -> dict[str, Any]:
    app = _application_record(application_id)
    if not app:
        return {"status": "error", "message": "Application does not exist."}
    if app["status"] == "submitted":
        return {"status": "blocked", "message": "This application is already marked submitted."}
    mode = str(app["mode"] or setting("mode", "review"))
    if mode == "review" and app["status"] != "approved":
        message = "Review mode requires approval before browser submission."
        log(message, "warning", {"application_id": application_id})
        return {"status": "blocked", "message": message}
    limit = int(setting("daily_application_limit", "10") or "10")
    count = submitted_today_count()
    if count >= limit:
        message = f"Daily application limit reached ({count}/{limit})."
        log(message, "warning", {"application_id": application_id})
        return {"status": "blocked", "message": message}
    if app["resume_compile_status"] != "compiled":
        message = "Compile and validate the tailored resume PDF before browser submission."
        log(message, "warning", {"application_id": application_id})
        return {"status": "blocked", "message": message}
    pdf_path = Path(str(app["resume_pdf_path"] or "")).resolve()
    expected_dir = application_dir(application_id).resolve()
    if (
        pdf_path.parent != expected_dir
        or pdf_path.name != "resume.pdf"
        or not pdf_path.is_file()
    ):
        message = "The compiled resume PDF is missing or outside the generated artifact directory."
        log(message, "warning", {"application_id": application_id})
        return {"status": "blocked", "message": message}
    existing = row(
        """
        SELECT * FROM application_tasks
        WHERE application_id = ? AND status IN ('queued', 'running', 'checkpoint')
        ORDER BY id DESC LIMIT 1
        """,
        (application_id,),
    )
    if existing:
        task = _task_dict(existing)
        task["message"] = (
            "This application already has a browser task waiting for action."
            if task["status"] == "checkpoint"
            else "This application is already queued for browser automation."
        )
        return task
    target_url = str(app["apply_url"] or app["url"])
    adapter = _adapter_name(target_url, str(app["source"] or ""))
    browser_session = (
        browser_sessions.ensure_session(adapter, target_url)
        if adapter in SUPPORTED_ADAPTERS
        else None
    )
    now = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO application_tasks(
              application_id, browser_session_id, adapter, target_url, mode, status,
              current_step, message, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?)
            """,
            (
                application_id,
                int(browser_session["id"]) if browser_session else None,
                adapter,
                target_url,
                mode,
                "Browser application queued.",
                now,
                now,
            ),
        )
        task_id = int(cursor.lastrowid)
        artifact_dir = GENERATED_DIR / "browser" / "tasks" / str(task_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        conn.execute(
            "UPDATE application_tasks SET artifact_dir = ? WHERE id = ?",
            (str(artifact_dir), task_id),
        )
    _record_task_event(
        task_id,
        "queued",
        f"Queued {adapter if adapter != 'unsupported' else 'unsupported-site'} browser application for {app['company']}.",
        meta={"application_id": application_id, "adapter": adapter},
    )
    _worker_event.set()
    return get_task(task_id) or {}


def _claim_next_task() -> dict[str, Any] | None:
    with connect() as conn:
        found = conn.execute(
            "SELECT * FROM application_tasks WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if not found:
            return None
        cursor = conn.execute(
            """
            UPDATE application_tasks
            SET status = 'running', current_step = 'starting', message = ?,
                attempt_count = attempt_count + 1,
                started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            ("Starting the browser application.", now_iso(), now_iso(), int(found["id"])),
        )
        if not cursor.rowcount:
            return None
    task = row("SELECT * FROM application_tasks WHERE id = ?", (int(found["id"]),))
    return task


def _normalize_question(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _question_for(field: dict[str, Any]) -> str:
    for key in ("label", "aria_label", "placeholder", "name"):
        value = str(field.get(key) or "").strip()
        if value:
            return value
    return f"Required {field.get('type') or field.get('tag') or 'field'}"


def _candidate_answers(app: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    candidate = structured_profile()
    contact = candidate.get("contact", {}) if isinstance(candidate.get("contact"), dict) else {}
    full_name = str(candidate.get("name") or "").strip()
    name_parts = full_name.split()
    links = [str(value) for value in contact.get("links", [])]
    statements = _json_value(app.get("statements"), [])
    statement_answers = {
        _normalize_question(item.get("question")): str(item.get("answer") or "")
        for item in statements
        if isinstance(item, dict) and item.get("answer")
    }
    rules = [
        {
            "question": _normalize_question(item["question"]),
            "answer": str(item["answer"]),
            "risky": bool(item["risky"]),
        }
        for item in rows("SELECT question, answer, risky FROM answer_rules ORDER BY updated_at DESC")
    ]
    return {
        "full_name": full_name,
        "first_name": name_parts[0] if name_parts else "",
        "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "email": str((contact.get("emails") or [""])[0]),
        "phone": str((contact.get("phones") or [""])[0]),
        "linkedin": next((value for value in links if "linkedin.com" in value.casefold()), ""),
        "github": next((value for value in links if "github.com" in value.casefold()), ""),
        "website": next(
            (value for value in links if "linkedin.com" not in value.casefold() and "github.com" not in value.casefold()),
            links[0] if links else "",
        ),
        "cover_letter": str(app.get("cover_letter") or ""),
        "statements": statement_answers,
        "rules": rules,
        "explicit": {
            _normalize_question(key): str(value)
            for key, value in dict(_json_value(task.get("answers_json"), {})).items()
        },
    }


def _rule_answer(question: str, answers: dict[str, Any]) -> tuple[str, bool]:
    normalized = _normalize_question(question)
    explicit = answers["explicit"]
    if normalized in explicit:
        return explicit[normalized], False
    for saved_question, answer in answers["statements"].items():
        if normalized == saved_question or (saved_question and saved_question in normalized):
            return answer, False
    for rule in answers["rules"]:
        saved_question = str(rule["question"])
        if normalized == saved_question or (
            len(saved_question) >= 12 and (saved_question in normalized or normalized in saved_question)
        ):
            return str(rule["answer"]), bool(rule["risky"])
    return "", False


def _identity_answer(question: str, answers: dict[str, Any]) -> str:
    normalized = _normalize_question(question)
    if re.search(r"\bfirst name\b", normalized):
        return str(answers["first_name"])
    if re.search(r"\blast name\b|\bsurname\b", normalized):
        return str(answers["last_name"])
    if re.search(r"\bfull name\b|\byour name\b|\bcandidate name\b", normalized):
        return str(answers["full_name"])
    if normalized in {"name", "name required"}:
        return str(answers["full_name"])
    if re.search(r"\be ?mail\b", normalized):
        return str(answers["email"])
    if re.search(r"\bphone\b|\bmobile\b", normalized):
        return str(answers["phone"])
    if "linkedin" in normalized:
        return str(answers["linkedin"])
    if "github" in normalized:
        return str(answers["github"])
    if re.search(r"\bportfolio\b|\bwebsite\b|\bpersonal url\b", normalized):
        return str(answers["website"])
    if "cover letter" in normalized:
        return str(answers["cover_letter"])
    return ""


FIELD_SNAPSHOT_SCRIPT = """
(elements) => elements.map((el, index) => {
  const byFor = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
  const wrapper = el.closest("label");
  const container = el.closest(".field, .application-question, [class*='field'], [class*='question']");
  const containerLabel = container ? container.querySelector("label, legend, .label") : null;
  const label = [byFor?.innerText, wrapper?.innerText, containerLabel?.innerText]
    .find((value) => value && value.trim()) || "";
  const rect = el.getBoundingClientRect();
  return {
    index,
    tag: el.tagName.toLowerCase(),
    type: (el.type || "").toLowerCase(),
    name: el.name || "",
    label: label.trim().replace(/\\s+/g, " ").slice(0, 500),
    aria_label: el.getAttribute("aria-label") || "",
    placeholder: el.getAttribute("placeholder") || "",
    required: Boolean(el.required || el.getAttribute("aria-required") === "true"),
    disabled: Boolean(el.disabled),
    visible: Boolean(rect.width || rect.height || el.type === "file"),
    value: el.type === "checkbox" || el.type === "radio" ? "" : (el.value || ""),
    checked: Boolean(el.checked),
    options: el.tagName === "SELECT"
      ? Array.from(el.options).map((option) => ({value: option.value, label: option.text})).slice(0, 100)
      : []
  };
})
"""


def _field_snapshot(page: Any) -> list[dict[str, Any]]:
    return list(page.locator("input, textarea, select").evaluate_all(FIELD_SNAPSHOT_SCRIPT))


def _public_snapshot(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "question": _question_for(field),
            "type": field.get("type") or field.get("tag"),
            "name": field.get("name") or "",
            "required": bool(field.get("required")),
            "filled": bool(field.get("value") or field.get("checked") or field.get("type") == "file"),
            "options": [item.get("label", "") for item in field.get("options", [])],
        }
        for field in fields
        if field.get("visible") and field.get("type") not in {"hidden", "submit", "button"}
    ]


def _select_option(locator: Any, answer: str, options: list[dict[str, str]]) -> bool:
    normalized = answer.casefold().strip()
    for option in options:
        if normalized in {str(option.get("label", "")).casefold().strip(), str(option.get("value", "")).casefold().strip()}:
            locator.select_option(value=str(option.get("value", "")))
            return True
    for option in options:
        if normalized and normalized in str(option.get("label", "")).casefold():
            locator.select_option(value=str(option.get("value", "")))
            return True
    return False


def _fill_control(page: Any, field: dict[str, Any], answer: str) -> bool:
    if not answer:
        return False
    locator = page.locator("input, textarea, select").nth(int(field["index"]))
    field_type = str(field.get("type") or "")
    if field["tag"] == "select":
        return _select_option(locator, answer, list(field.get("options") or []))
    if field_type == "checkbox":
        if answer.casefold() in {"yes", "true", "1", "checked", "agree"}:
            locator.check()
            return True
        return False
    if field_type == "radio":
        value = str(field.get("value") or field.get("label") or "")
        if answer.casefold().strip() in value.casefold():
            locator.check()
            return True
        return False
    locator.fill(answer)
    return True


def _save_screenshot(page: Any, task: dict[str, Any], name: str) -> str:
    task_dir = Path(str(task["artifact_dir"])).resolve()
    expected_root = (GENERATED_DIR / "browser" / "tasks").resolve()
    if expected_root not in task_dir.parents:
        raise RuntimeError("Browser task artifact directory is outside the generated root")
    task_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-z0-9_.-]+", "-", name.casefold()).strip("-")
    path = task_dir / f"{safe_name}.png"
    page.screenshot(path=str(path), full_page=True)
    screenshots = list(_json_value(task.get("screenshots_json"), []))
    if path.name not in screenshots:
        screenshots.append(path.name)
    task["screenshots_json"] = json.dumps(screenshots)
    with connect() as conn:
        conn.execute(
            "UPDATE application_tasks SET screenshots_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(screenshots), now_iso(), int(task["id"])),
        )
    return path.name


def _page_has_captcha(page: Any) -> bool:
    return page.locator(
        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], "
        "[class*='captcha' i], [id*='captcha' i]"
    ).count() > 0


def _page_requires_login(page: Any) -> bool:
    return page.locator("input[type='password']").count() > 0


def _find_form_surface(page: Any) -> Any:
    candidates = [page, *[frame for frame in page.frames if frame != page.main_frame]]
    for candidate in candidates:
        if candidate.locator("input[type='file']").count():
            return candidate
    for candidate in candidates:
        has_email = candidate.locator("input[type='email'], input[name*='email' i]").count()
        has_name = candidate.locator(
            "input[name*='first_name' i], input[name*='firstName' i], input[name='name']"
        ).count()
        if has_email and has_name:
            return candidate
    for candidate in candidates:
        if candidate.locator("form input[required], form textarea[required], form select[required]").count():
            return candidate
    return page


def _surface_has_application_fields(surface: Any) -> bool:
    if surface.locator("input[type='file']").count():
        return True
    has_email = surface.locator("input[type='email'], input[name*='email' i]").count()
    has_name = surface.locator(
        "input[name*='first_name' i], input[name*='firstName' i], input[name='name']"
    ).count()
    return bool(has_email and has_name)


def _click_and_settle(page: Any, control: Any) -> None:
    control.click(timeout=15_000)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(300)


def _open_application_form(page: Any, adapter: str, task_id: int) -> Any:
    surface = _find_form_surface(page)
    if _surface_has_application_fields(surface):
        return surface
    special_selectors = {
        "greenhouse": "a[href*='#app']",
        "lever": "a.postings-btn, a[href$='/apply'], a[href*='/apply?']",
        "ashby": "a[href$='/apply'], a[href*='/apply?']",
        "smartrecruiters": "",
        "workday": "[data-automation-id='jobPostingApplyButton']",
    }
    labels = {
        "greenhouse": r"apply for this job|apply now|apply",
        "lever": r"apply for this job|apply now|apply",
        "ashby": r"apply for this job|apply now|apply",
        "smartrecruiters": r"i['’]m interested|apply now|apply",
        "workday": r"apply now|apply",
    }
    special = special_selectors.get(adapter, "")
    apply_control = page.locator(special).first if special else None
    if apply_control is None or not apply_control.count():
        apply_control = page.locator("button, a").filter(
            has_text=re.compile(
                rf"^\s*(?:{labels.get(adapter, 'apply')})\s*$",
                re.IGNORECASE,
            )
        ).first
    if apply_control.count():
        _record_task_event(task_id, "opening", "Opening the posting's application form.")
        _click_and_settle(page, apply_control)
        if adapter == "workday":
            manual = page.locator("[data-automation-id='applyManually']").first
            if not manual.count():
                manual = page.locator("button, a").filter(
                    has_text=re.compile(r"^\s*apply manually\s*$", re.IGNORECASE)
                ).first
            if manual.count():
                _record_task_event(
                    task_id,
                    "opening",
                    "Choosing Workday's manual application path.",
                )
                _click_and_settle(page, manual)
        return _find_form_surface(page)
    return surface


def _submit_locator(page: Any, adapter: str) -> Any:
    buttons = page.locator("button").filter(
        has_text=re.compile(
            r"^\s*(?:submit|submit application|send application)\s*$",
            re.IGNORECASE,
        )
    )
    if buttons.count():
        return buttons.first
    if adapter == "workday":
        workday_submit = page.locator("[data-automation-id='submitButton']").first
        if workday_submit.count():
            return workday_submit
    return page.locator("input[type='submit'][value*='Submit' i]").first


def _continue_locator(page: Any, adapter: str) -> Any:
    labels = {
        "smartrecruiters": r"next|continue|save and continue",
        "workday": r"next|save and continue|save & continue",
        "ashby": r"next|continue",
    }
    choices = labels.get(adapter)
    if not choices:
        return None
    return page.locator("button, a").filter(
        has_text=re.compile(rf"^\s*(?:{choices})\s*$", re.IGNORECASE)
    ).first


def _checkpoint(
    kind: str,
    message: str,
    fields: list[dict[str, Any]] | None = None,
    **extra: object,
) -> dict[str, Any]:
    return {
        "status": "checkpoint",
        "message": message,
        "checkpoint_kind": kind,
        "checkpoint": {"fields": fields or [], **extra},
    }


def _fill_application_step(
    page: Any,
    surface: Any,
    task: dict[str, Any],
    app: dict[str, Any],
    answers: dict[str, Any],
    step_number: int,
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
    fields = _field_snapshot(surface)
    missing: list[dict[str, Any]] = []
    sensitive: list[dict[str, Any]] = []
    resume_uploaded = False
    cover_letter_path = Path(str(task["artifact_dir"])) / "cover-letter.txt"
    cover_letter_path.write_text(str(app.get("cover_letter") or ""), encoding="utf-8")

    for field in fields:
        if field.get("disabled") or not field.get("visible"):
            continue
        field_type = str(field.get("type") or "")
        if field_type in {"hidden", "submit", "button", "reset", "image"}:
            continue
        question = _question_for(field)
        if field_type == "file":
            locator = surface.locator("input, textarea, select").nth(int(field["index"]))
            normalized = _normalize_question(question)
            if "cover letter" in normalized:
                locator.set_input_files(str(cover_letter_path))
            elif "resume" in normalized or "cv" in normalized or not resume_uploaded:
                locator.set_input_files(str(Path(str(app["resume_pdf_path"])).resolve()))
                resume_uploaded = True
            elif field.get("required"):
                missing.append({"question": question, "type": "file", "options": []})
            continue
        if field.get("value") or field.get("checked"):
            continue
        identity_answer = _identity_answer(question, answers)
        saved_answer, saved_risky = _rule_answer(question, answers)
        answer = identity_answer or saved_answer
        risky = bool(RISKY_PATTERNS.search(question)) or saved_risky
        explicitly_answered = _normalize_question(question) in answers["explicit"]
        allow_sensitive = setting("browser_allow_sensitive_answers", "false") == "true"
        if risky and answer and not explicitly_answered and not allow_sensitive:
            sensitive.append(
                {
                    "question": question,
                    "type": field_type or field.get("tag"),
                    "options": [item.get("label", "") for item in field.get("options", [])],
                    "suggested_answer": answer,
                }
            )
            continue
        if answer:
            try:
                if _fill_control(surface, field, answer):
                    continue
            except Exception:
                pass
        if field.get("required"):
            target = {
                "question": question,
                "type": field_type or field.get("tag"),
                "options": [item.get("label", "") for item in field.get("options", [])],
            }
            (sensitive if risky else missing).append(target)

    public_snapshot = _public_snapshot(_field_snapshot(surface))
    for item in public_snapshot:
        item["step"] = step_number
    screenshot = _save_screenshot(page, task, f"02-step-{step_number}-filled")
    if sensitive:
        return (
            public_snapshot,
            screenshot,
            _checkpoint(
                "sensitive_question",
                "Sensitive application questions need your explicit answer before the task can continue.",
                sensitive,
                screenshot=screenshot,
                step=step_number,
            ),
        )
    if missing:
        return (
            public_snapshot,
            screenshot,
            _checkpoint(
                "unknown_field",
                "Required fields remain unanswered. Add answers, then continue the task.",
                missing,
                screenshot=screenshot,
                step=step_number,
            ),
        )
    return public_snapshot, screenshot, None


def _confirmation_text(page: Any) -> str:
    content: list[str] = []
    for frame in page.frames:
        try:
            content.append(frame.locator("body").inner_text(timeout=5_000))
        except Exception:
            continue
    return "\n".join(content)


def _run_browser_task(task: dict[str, Any], app: dict[str, Any]) -> dict[str, Any]:
    if task["adapter"] not in SUPPORTED_ADAPTERS:
        return _checkpoint(
            "unsupported_site",
            "This site is not a supported ATS application form. Open it for manual submission.",
            target_url=task["target_url"],
        )
    if not playwright_available():
        raise RuntimeError("Playwright is not installed. Run the local setup before browser automation.")

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    task_id = int(task["id"])
    session = browser_sessions.session_for_task(task)
    session_id = int(session["id"])
    if not browser_sessions.begin_automation(session_id, task_id):
        return _checkpoint(
            "session_busy",
            "This ATS browser session is already open. The task remains paused until that window closes.",
            target_url=task["target_url"],
            browser_session_id=session_id,
        )
    _record_task_event(
        task_id,
        "opening",
        f"Opening the {task['adapter'].title()} application form with its saved local session.",
    )
    with sync_playwright() as playwright:
        try:
            context = browser_sessions.launch_context(
                playwright,
                session,
                headless=setting("browser_headless", "true") == "true",
            )
        except Exception as exc:
            browser_sessions.finish_automation(session_id, error=str(exc))
            raise
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(str(task["target_url"]), wait_until="domcontentloaded", timeout=45_000)
            _save_screenshot(page, task, "01-opened")
            if _page_has_captcha(page):
                return _checkpoint(
                    "captcha",
                    "The site presented a CAPTCHA. Automation stopped without attempting to bypass it.",
                    target_url=task["target_url"],
                )
            surface = _open_application_form(page, str(task["adapter"]), task_id)
            if _page_has_captcha(page):
                return _checkpoint(
                    "captcha",
                    "The application form presented a CAPTCHA. Automation stopped without attempting to bypass it.",
                    target_url=page.url,
                )
            if _page_requires_login(surface):
                browser_sessions.mark_needs_login(session_id, task_id)
                return _checkpoint(
                    "login",
                    "The application requires a login. Open the guided sign-in window to continue with this saved session.",
                    target_url=page.url,
                    browser_session_id=session_id,
                )
            answers = _candidate_answers(app, task)
            all_snapshots: list[dict[str, Any]] = []
            for step_number in range(1, MAX_FORM_STEPS + 1):
                surface = _find_form_surface(page)
                if _page_has_captcha(page):
                    return _checkpoint(
                        "captcha",
                        "The application presented a CAPTCHA. Automation stopped without attempting to bypass it.",
                        target_url=page.url,
                        step=step_number,
                    )
                if _page_requires_login(surface):
                    browser_sessions.mark_needs_login(session_id, task_id)
                    return _checkpoint(
                        "login",
                        "The application requires a login. Open the guided sign-in window to continue with this saved session.",
                        target_url=page.url,
                        step=step_number,
                        browser_session_id=session_id,
                    )
                _record_task_event(
                    task_id,
                    "filling",
                    f"Filling known fields on application step {step_number}.",
                )
                snapshot, screenshot, checkpoint = _fill_application_step(
                    page,
                    surface,
                    task,
                    app,
                    answers,
                    step_number,
                )
                all_snapshots.extend(snapshot)
                with connect() as conn:
                    conn.execute(
                        "UPDATE application_tasks SET form_snapshot_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(all_snapshots), now_iso(), task_id),
                    )
                if checkpoint:
                    return checkpoint

                submit = _submit_locator(surface, str(task["adapter"]))
                if submit.count():
                    submit_enabled = setting("browser_submit_enabled", "false") == "true"
                    autonomous = task["mode"] == "rules_autonomous" and submit_enabled
                    explicitly_approved = bool(task.get("final_submit_approved"))
                    if not autonomous and not explicitly_approved:
                        return _checkpoint(
                            "final_review",
                            "The form is filled and ready. Review the screenshot, then explicitly approve final submission.",
                            screenshot=screenshot,
                            target_url=page.url,
                            step=step_number,
                        )
                    limit = int(setting("daily_application_limit", "10") or "10")
                    count = submitted_today_count()
                    if count >= limit:
                        return _checkpoint(
                            "daily_limit",
                            f"Final submission stopped because the daily limit was reached ({count}/{limit}).",
                        )
                    _record_task_event(
                        task_id,
                        "submitting",
                        "Clicking the final application submit control.",
                    )
                    with connect() as conn:
                        conn.execute(
                            "UPDATE application_tasks SET submit_started_at = ?, updated_at = ? WHERE id = ?",
                            (now_iso(), now_iso(), task_id),
                        )
                    submit.click(timeout=15_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except PlaywrightTimeoutError:
                        pass
                    confirmation = _confirmation_text(page)
                    final_screenshot = _save_screenshot(page, task, "03-submitted")
                    if not SUCCESS_PATTERNS.search(confirmation):
                        return _checkpoint(
                            "submission_uncertain",
                            "The submit control was clicked, but a confirmation could not be verified. Check the site before retrying.",
                            screenshot=final_screenshot,
                            target_url=page.url,
                        )
                    return {
                        "status": "submitted",
                        "message": "The application was submitted and the confirmation page was verified.",
                        "result": {"confirmation_url": page.url, "screenshot": final_screenshot},
                    }

                continue_control = _continue_locator(surface, str(task["adapter"]))
                if continue_control is not None and continue_control.count():
                    _record_task_event(
                        task_id,
                        "advancing",
                        f"Application step {step_number} is complete; advancing to the next step.",
                    )
                    _click_and_settle(page, continue_control)
                    continue
                if not all_snapshots:
                    return _checkpoint(
                        "unsupported_form",
                        "No supported application fields were detected. Open this form for manual submission.",
                        screenshot=screenshot,
                        target_url=page.url,
                    )
                return _checkpoint(
                    "submit_control",
                    "The visible form step was filled, but no supported next or final submit control was found.",
                    screenshot=screenshot,
                    target_url=page.url,
                    step=step_number,
                )
            return _checkpoint(
                "step_limit",
                f"Automation stopped after {MAX_FORM_STEPS} application steps to prevent an unbounded workflow.",
                target_url=page.url,
            )
        except Exception as exc:
            browser_sessions.finish_automation(session_id, error=str(exc))
            raise
        finally:
            try:
                context.close()
            finally:
                browser_sessions.finish_automation(session_id)


def process_next_task(
    runner: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    task = _claim_next_task()
    if not task:
        return None
    task_id = int(task["id"])
    application_id = int(task["application_id"])
    _record_task_event(task_id, "starting", "The background application worker claimed this task.")
    try:
        app = _application_record(application_id)
        if not app:
            raise RuntimeError("The application disappeared before browser automation started.")
        if app["status"] == "submitted":
            raise RuntimeError("The application is already marked submitted.")
        limit = int(setting("daily_application_limit", "10") or "10")
        count = submitted_today_count()
        if count >= limit:
            outcome = _checkpoint(
                "daily_limit",
                f"Application worker stopped because the daily limit was reached ({count}/{limit}).",
            )
        else:
            outcome = (runner or _run_browser_task)(task, app)
        status = str(outcome.get("status") or "failed")
        message = str(outcome.get("message") or "Browser application task finished.")
        if status == "submitted":
            from .applications import mark_application_submitted

            mark_application_submitted(application_id)
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE application_tasks
                    SET status = 'submitted', current_step = 'submitted', message = ?,
                        result_json = ?, checkpoint_kind = '', checkpoint_json = '{}',
                        completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        message,
                        json.dumps(outcome.get("result") or {}),
                        now_iso(),
                        now_iso(),
                        task_id,
                    ),
                )
            _record_task_event(task_id, "submitted", message)
        elif status == "checkpoint":
            kind = str(outcome.get("checkpoint_kind") or "review")
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE application_tasks
                    SET status = 'checkpoint', current_step = 'checkpoint', message = ?,
                        checkpoint_kind = ?, checkpoint_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        message,
                        kind,
                        json.dumps(outcome.get("checkpoint") or {}),
                        now_iso(),
                        task_id,
                    ),
                )
            _record_task_event(task_id, "checkpoint", message, "warning", {"kind": kind})
        else:
            raise RuntimeError(message)
    except Exception as exc:
        current = row(
            "SELECT submit_started_at FROM application_tasks WHERE id = ?",
            (task_id,),
        )
        submit_started = bool(current and current["submit_started_at"])
        status = "checkpoint" if submit_started else "failed"
        kind = "submission_uncertain" if submit_started else ""
        message = (
            f"The browser stopped after final submission began: {exc}. Check the site before retrying."
            if submit_started
            else f"Browser application task failed: {exc}"
        )
        with connect() as conn:
            conn.execute(
                """
                UPDATE application_tasks
                SET status = ?, current_step = ?, message = ?, checkpoint_kind = ?,
                    checkpoint_json = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    "checkpoint" if submit_started else "failed",
                    message,
                    kind,
                    json.dumps({"target_url": task["target_url"]}) if submit_started else "{}",
                    "" if submit_started else now_iso(),
                    now_iso(),
                    task_id,
                ),
            )
        _record_task_event(
            task_id,
            "checkpoint" if submit_started else "failed",
            message,
            "error",
            {"kind": kind} if kind else {},
        )
    return get_task(task_id)


def resolve_checkpoint(
    task_id: int,
    answers: dict[str, object] | None = None,
    approve_submit: bool = False,
    save_rules: bool = True,
) -> dict[str, Any]:
    task = row("SELECT * FROM application_tasks WHERE id = ?", (task_id,))
    if not task:
        raise ValueError("Browser application task does not exist")
    if task["status"] != "checkpoint":
        raise ValueError("Only checkpointed browser tasks can continue")
    non_resumable = {
        "submission_uncertain",
        "unsupported_site",
        "unsupported_form",
        "submit_control",
        "step_limit",
        "captcha",
        "login",
        "session_busy",
    }
    if task["checkpoint_kind"] in non_resumable:
        raise ValueError(
            "This checkpoint cannot be retried automatically. Open the application and verify it manually."
        )
    if task["checkpoint_kind"] == "final_review" and not approve_submit:
        raise ValueError("Final review requires explicit submit approval")
    merged = dict(_json_value(task["answers_json"], {}))
    for question, answer in (answers or {}).items():
        cleaned_question = str(question).strip()
        cleaned_answer = str(answer).strip()
        if cleaned_question and cleaned_answer:
            merged[cleaned_question] = cleaned_answer
            if save_rules:
                from .applications import save_answer_rule

                save_answer_rule(cleaned_question, cleaned_answer)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE application_tasks
            SET status = 'queued', current_step = 'queued', message = ?,
                answers_json = ?, final_submit_approved = ?,
                checkpoint_kind = '', checkpoint_json = '{}', updated_at = ?
            WHERE id = ?
            """,
            (
                "Checkpoint resolved. Browser application re-queued.",
                json.dumps(merged),
                1 if approve_submit or task["final_submit_approved"] else 0,
                now,
                task_id,
            ),
        )
    _record_task_event(task_id, "queued", "Checkpoint resolved. Browser application re-queued.")
    _worker_event.set()
    return get_task(task_id) or {}


def resume_login_tasks(session_id: int) -> list[int]:
    waiting = rows(
        """
        SELECT id FROM application_tasks
        WHERE browser_session_id = ? AND status = 'checkpoint'
          AND checkpoint_kind IN ('login', 'session_busy')
        ORDER BY id
        """,
        (session_id,),
    )
    task_ids = [int(item["id"]) for item in waiting]
    if not task_ids:
        return []
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE application_tasks
            SET status = 'queued', current_step = 'queued', message = ?,
                checkpoint_kind = '', checkpoint_json = '{}', updated_at = ?
            WHERE browser_session_id = ? AND status = 'checkpoint'
              AND checkpoint_kind IN ('login', 'session_busy')
            """,
            (
                "Manual sign-in completed. Browser application re-queued with the saved session.",
                now,
                session_id,
            ),
        )
    for task_id in task_ids:
        _record_task_event(
            task_id,
            "queued",
            "Manual sign-in completed. Browser application re-queued with the saved session.",
        )
    _worker_event.set()
    return task_ids


def cancel_task(task_id: int) -> dict[str, Any]:
    task = row("SELECT * FROM application_tasks WHERE id = ?", (task_id,))
    if not task:
        raise ValueError("Browser application task does not exist")
    if task["status"] not in {"queued", "checkpoint"}:
        raise ValueError("Only queued or checkpointed browser tasks can be cancelled")
    session_id = int(task.get("browser_session_id") or 0)
    session = browser_sessions.get_session(session_id) if session_id else None
    if session and session["active"]:
        raise ValueError("Cancel the active sign-in handoff before cancelling this task")
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE application_tasks
            SET status = 'cancelled', current_step = 'cancelled', message = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('queued', 'checkpoint')
            """,
            ("Browser application task cancelled.", now_iso(), now_iso(), task_id),
        )
        if not cursor.rowcount:
            raise ValueError("The browser task started before cancellation could be applied")
    _record_task_event(task_id, "cancelled", "Browser application task cancelled.", "warning")
    return get_task(task_id) or {}


def start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        browser_sessions.recover_interrupted_handoffs()
        with connect() as conn:
            conn.execute(
                """
                UPDATE application_tasks
                SET status = 'checkpoint', current_step = 'checkpoint',
                    checkpoint_kind = 'submission_uncertain',
                    checkpoint_json = json_object('target_url', target_url),
                    message = ?, updated_at = ?
                WHERE status = 'running' AND submit_started_at <> ''
                """,
                (
                    "The local server stopped during final submission. Verify the site before taking another action.",
                    now_iso(),
                ),
            )
            conn.execute(
                """
                UPDATE application_tasks
                SET status = 'queued', current_step = 'queued', message = ?, updated_at = ?
                WHERE status = 'running' AND submit_started_at = ''
                """,
                ("Recovered before final submission after a local server restart.", now_iso()),
            )
        _worker_started = True

        def loop() -> None:
            while True:
                try:
                    processed = process_next_task()
                except Exception as exc:
                    log(f"Application worker error: {exc}", "error")
                    processed = None
                if not processed:
                    _worker_event.wait(timeout=3)
                    _worker_event.clear()

        threading.Thread(
            target=loop,
            daemon=True,
            name="applyforme-application-worker",
        ).start()
