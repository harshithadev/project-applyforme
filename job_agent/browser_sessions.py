from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import BROWSER_SESSIONS_DIR
from .db import connect, log, now_iso, row, rows, setting


ACTIVE_STATUSES = frozenset(
    {"in_use", "handoff_opening", "awaiting_user", "handoff_closing"}
)


@dataclass
class HandoffControl:
    session_id: int
    task_id: int
    event: threading.Event = field(default_factory=threading.Event)
    action: str = ""


_handoff_lock = threading.Lock()
_handoffs: dict[int, HandoffControl] = {}


def _session_identity(adapter: str, target_url: str) -> tuple[str, str]:
    parsed = urlparse(target_url)
    hostname = (parsed.netloc or parsed.hostname or "unknown-host").casefold()
    identity = f"{adapter.casefold()}:{hostname}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{adapter.casefold()}-{digest}", hostname


def _profile_dir(session_key: str) -> Path:
    safe_key = re.sub(r"[^a-z0-9-]+", "-", session_key.casefold()).strip("-")
    if not safe_key:
        raise ValueError("Browser session key is invalid")
    return BROWSER_SESSIONS_DIR / safe_key


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    profile = _profile_dir(str(session["session_key"]))
    return {
        **session,
        "active": str(session["status"]) in ACTIVE_STATUSES,
        "can_clear": str(session["status"]) not in ACTIVE_STATUSES,
        "profile_present": profile.is_dir(),
        "storage": "local_chromium_profile",
    }


def ensure_session(adapter: str, target_url: str) -> dict[str, Any]:
    session_key, hostname = _session_identity(adapter, target_url)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO browser_sessions(
              session_key, adapter, hostname, status, target_url, message,
              created_at, updated_at
            )
            VALUES(?, ?, ?, 'new', ?, ?, ?, ?)
            """,
            (
                session_key,
                adapter,
                hostname,
                target_url,
                "A local browser session will be created when this site is opened.",
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE browser_sessions
            SET target_url = ?, adapter = ?, hostname = ?, updated_at = ?
            WHERE session_key = ?
            """,
            (target_url, adapter, hostname, now, session_key),
        )
    found = row("SELECT * FROM browser_sessions WHERE session_key = ?", (session_key,))
    if not found:
        raise RuntimeError("Could not create the local browser session")
    _secure_directory(BROWSER_SESSIONS_DIR)
    _secure_directory(_profile_dir(session_key))
    return _public_session(found)


def attach_task(task_id: int, adapter: str, target_url: str) -> dict[str, Any]:
    session = ensure_session(adapter, target_url)
    with connect() as conn:
        conn.execute(
            "UPDATE application_tasks SET browser_session_id = ?, updated_at = ? WHERE id = ?",
            (int(session["id"]), now_iso(), task_id),
        )
    return session


def session_for_task(task: dict[str, Any]) -> dict[str, Any]:
    session_id = int(task.get("browser_session_id") or 0)
    found = (
        row("SELECT * FROM browser_sessions WHERE id = ?", (session_id,))
        if session_id
        else None
    )
    if found:
        return _public_session(found)
    return attach_task(
        int(task["id"]),
        str(task.get("adapter") or "unsupported"),
        str(task.get("target_url") or ""),
    )


def get_session(session_id: int) -> dict[str, Any] | None:
    found = row("SELECT * FROM browser_sessions WHERE id = ?", (session_id,))
    return _public_session(found) if found else None


def list_sessions() -> list[dict[str, Any]]:
    return [
        _public_session(item)
        for item in rows(
            "SELECT * FROM browser_sessions ORDER BY updated_at DESC, id DESC LIMIT 100"
        )
    ]


def session_status() -> dict[str, object]:
    sessions = list_sessions()
    return {
        "total": len(sessions),
        "ready": sum(item["status"] == "ready" for item in sessions),
        "needs_login": sum(
            item["status"] in {"needs_login", "interrupted", "error"}
            for item in sessions
        ),
        "active": sum(bool(item["active"]) for item in sessions),
    }


def _write_profile_preferences(profile_dir: Path) -> None:
    default_dir = profile_dir / "Default"
    _secure_directory(default_dir)
    preferences_path = default_dir / "Preferences"
    preferences: dict[str, Any] = {}
    if preferences_path.is_file():
        try:
            loaded = json.loads(preferences_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                preferences = loaded
        except (OSError, json.JSONDecodeError):
            preferences = {}
    profile_preferences = preferences.get("profile")
    if not isinstance(profile_preferences, dict):
        profile_preferences = {}
        preferences["profile"] = profile_preferences
    autofill_preferences = preferences.get("autofill")
    if not isinstance(autofill_preferences, dict):
        autofill_preferences = {}
        preferences["autofill"] = autofill_preferences
    preferences["credentials_enable_service"] = False
    profile_preferences["password_manager_enabled"] = False
    autofill_preferences["profile_enabled"] = False
    autofill_preferences["credit_card_enabled"] = False
    preferences_path.write_text(json.dumps(preferences), encoding="utf-8")
    preferences_path.chmod(0o600)


def launch_context(
    playwright: Any,
    session: dict[str, Any],
    *,
    headless: bool,
) -> Any:
    profile_dir = _profile_dir(str(session["session_key"]))
    _secure_directory(BROWSER_SESSIONS_DIR)
    _secure_directory(profile_dir)
    _write_profile_preferences(profile_dir)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": 1440, "height": 1000},
        args=[
            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
        ],
    )


def begin_automation(session_id: int, task_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            """
            UPDATE browser_sessions
            SET status = 'in_use', active_task_id = ?, message = ?, updated_at = ?
            WHERE id = ?
              AND status NOT IN ('in_use', 'handoff_opening', 'awaiting_user', 'handoff_closing')
            """,
            (
                task_id,
                "The background worker is using this local browser session.",
                now_iso(),
                session_id,
            ),
        )
    return bool(cursor.rowcount)


def finish_automation(session_id: int, *, error: str = "") -> None:
    now = now_iso()
    with connect() as conn:
        if error:
            conn.execute(
                """
                UPDATE browser_sessions
                SET status = 'error', active_task_id = NULL, message = ?,
                    last_used_at = ?, updated_at = ?
                WHERE id = ? AND status = 'in_use'
                """,
                (f"Browser session failed: {error}", now, now, session_id),
            )
        else:
            conn.execute(
                """
                UPDATE browser_sessions
                SET status = 'ready', active_task_id = NULL, message = ?,
                    last_used_at = ?, updated_at = ?
                WHERE id = ? AND status = 'in_use'
                """,
                (
                    "The local browser session is saved and available for future applications.",
                    now,
                    now,
                    session_id,
                ),
            )


def mark_needs_login(session_id: int, task_id: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE browser_sessions
            SET status = 'needs_login', active_task_id = ?, message = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                task_id,
                "This ATS session needs a manual sign-in.",
                now_iso(),
                session_id,
            ),
        )


def _set_session_state(
    session_id: int,
    status: str,
    message: str,
    *,
    active_task_id: int | None,
    verified: bool = False,
) -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute(
            """
            UPDATE browser_sessions
            SET status = ?, active_task_id = ?, message = ?,
                last_verified_at = CASE WHEN ? THEN ? ELSE last_verified_at END,
                last_used_at = CASE WHEN ? THEN ? ELSE last_used_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                active_task_id,
                message,
                1 if verified else 0,
                now,
                1 if verified else 0,
                now,
                now,
                session_id,
            ),
        )


def _prepare_handoff(task_id: int) -> tuple[dict[str, Any], HandoffControl]:
    task = row("SELECT * FROM application_tasks WHERE id = ?", (task_id,))
    if not task:
        raise ValueError("Browser application task does not exist")
    if task["status"] != "checkpoint" or task["checkpoint_kind"] != "login":
        raise ValueError("Only a browser task waiting for login can start a sign-in handoff")
    session = session_for_task(task)
    try:
        checkpoint = json.loads(str(task.get("checkpoint_json") or "{}"))
    except json.JSONDecodeError:
        checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    session["target_url"] = str(
        checkpoint.get("target_url") or task.get("target_url") or ""
    )
    session_id = int(session["id"])
    with _handoff_lock:
        if session_id in _handoffs or str(session["status"]) in ACTIVE_STATUSES:
            raise ValueError("A browser window is already using this local session")
        control = HandoffControl(session_id=session_id, task_id=task_id)
        _handoffs[session_id] = control
    _set_session_state(
        session_id,
        "handoff_opening",
        "Opening a visible browser window for manual sign-in.",
        active_task_id=task_id,
    )
    return session, control


def _visible_password_field(page: Any) -> bool:
    try:
        return page.locator("input[type='password']:visible").count() > 0
    except Exception:
        return True


def _active_page(context: Any) -> Any | None:
    pages = [
        page
        for page in context.pages
        if str(getattr(page, "url", "") or "") not in {"", "about:blank"}
    ]
    return pages[-1] if pages else None


def _execute_handoff(
    session: dict[str, Any],
    control: HandoffControl,
    *,
    interaction: Callable[[Any], None] | None = None,
    headless: bool = False,
) -> None:
    from playwright.sync_api import sync_playwright

    from . import automation

    session_id = control.session_id
    task_id = control.task_id
    context = None
    try:
        with sync_playwright() as playwright:
            context = launch_context(playwright, session, headless=headless)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                str(session["target_url"]),
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            _set_session_state(
                session_id,
                "awaiting_user",
                "Sign in in the browser window, then confirm completion in ApplyForMe.",
                active_task_id=task_id,
            )
            automation._record_task_event(
                task_id,
                "login",
                "A visible browser window is waiting for manual sign-in.",
            )
            if interaction:
                interaction(page)
                control.action = "complete"
            else:
                try:
                    timeout_minutes = int(
                        setting("browser_login_timeout_minutes", "15") or "15"
                    )
                except ValueError:
                    timeout_minutes = 15
                deadline = time.monotonic() + max(1, min(timeout_minutes, 60)) * 60
                while not control.event.wait(timeout=1):
                    if not context.pages:
                        control.action = "closed"
                        break
                    if time.monotonic() >= deadline:
                        control.action = "timeout"
                        break
            if control.action != "complete":
                reasons = {
                    "cancel": "Manual sign-in was cancelled. The application remains paused.",
                    "closed": "The sign-in window closed before completion was confirmed.",
                    "timeout": "The sign-in window timed out. The application remains paused.",
                }
                message = reasons.get(
                    control.action,
                    "Manual sign-in ended before completion was confirmed.",
                )
                _set_session_state(
                    session_id,
                    "needs_login",
                    message,
                    active_task_id=task_id,
                )
                automation._record_task_event(
                    task_id,
                    "login",
                    message,
                    "warning",
                )
                return
            _set_session_state(
                session_id,
                "handoff_closing",
                "Verifying the signed-in browser state and closing the handoff window.",
                active_task_id=task_id,
            )
            active_page = _active_page(context)
            if active_page is None or _visible_password_field(active_page):
                message = (
                    "The browser still appears to be on a sign-in screen. "
                    "The application remains paused."
                )
                _set_session_state(
                    session_id,
                    "needs_login",
                    message,
                    active_task_id=task_id,
                )
                automation._record_task_event(
                    task_id,
                    "login",
                    message,
                    "warning",
                )
                return
            context.close()
            context = None
        _set_session_state(
            session_id,
            "ready",
            "Manual sign-in completed. The local browser session is ready.",
            active_task_id=None,
            verified=True,
        )
        automation.resume_login_tasks(session_id)
        log(
            f"Saved a local {session['adapter']} browser session after manual sign-in.",
            meta={"browser_session_id": session_id, "application_task_id": task_id},
        )
    except Exception as exc:
        message = f"Manual sign-in browser failed: {exc}"
        _set_session_state(
            session_id,
            "error",
            message,
            active_task_id=task_id,
        )
        automation._record_task_event(task_id, "login", message, "error")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        with _handoff_lock:
            _handoffs.pop(session_id, None)


def start_login_handoff(task_id: int) -> dict[str, Any]:
    session, control = _prepare_handoff(task_id)
    threading.Thread(
        target=_execute_handoff,
        args=(session, control),
        daemon=True,
        name=f"applyforme-login-handoff-{session['id']}",
    ).start()
    return get_session(int(session["id"])) or {}


def _run_login_handoff_for_test(
    task_id: int,
    interaction: Callable[[Any], None],
) -> dict[str, Any]:
    session, control = _prepare_handoff(task_id)
    _execute_handoff(
        session,
        control,
        interaction=interaction,
        headless=True,
    )
    return get_session(int(session["id"])) or {}


def complete_login_handoff(session_id: int) -> dict[str, Any]:
    with _handoff_lock:
        control = _handoffs.get(session_id)
        if not control:
            raise ValueError("No active sign-in window exists for this session")
        if control.action:
            raise ValueError("A sign-in handoff decision has already been received")
        control.action = "complete"
        control.event.set()
    _set_session_state(
        session_id,
        "handoff_closing",
        "Sign-in completion received. Verifying the browser state.",
        active_task_id=control.task_id,
    )
    return get_session(session_id) or {}


def cancel_login_handoff(session_id: int) -> dict[str, Any]:
    with _handoff_lock:
        control = _handoffs.get(session_id)
        if not control:
            raise ValueError("No active sign-in window exists for this session")
        if control.action:
            raise ValueError("A sign-in handoff decision has already been received")
        control.action = "cancel"
        control.event.set()
    _set_session_state(
        session_id,
        "handoff_closing",
        "Sign-in cancellation received. Closing the browser window.",
        active_task_id=control.task_id,
    )
    return get_session(session_id) or {}


def clear_session(session_id: int) -> dict[str, Any]:
    session = get_session(session_id)
    if not session:
        raise ValueError("Browser session does not exist")
    if session["active"]:
        raise ValueError("Close the active browser session before clearing it")
    profile = _profile_dir(str(session["session_key"])).resolve()
    root = BROWSER_SESSIONS_DIR.resolve()
    if profile.parent != root:
        raise RuntimeError("Browser session directory is outside the local session root")
    if profile.exists():
        shutil.rmtree(profile)
    _set_session_state(
        session_id,
        "cleared",
        "Local cookies and site data were cleared for this browser session.",
        active_task_id=None,
    )
    with connect() as conn:
        conn.execute(
            "UPDATE browser_sessions SET last_verified_at = '', last_used_at = '' WHERE id = ?",
            (session_id,),
        )
    log(
        f"Cleared the local {session['adapter']} browser session for {session['hostname']}.",
        "warning",
        {"browser_session_id": session_id},
    )
    return get_session(session_id) or {}


def recover_interrupted_handoffs() -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE browser_sessions
            SET status = 'interrupted', active_task_id = NULL, message = ?, updated_at = ?
            WHERE status IN ('in_use', 'handoff_opening', 'awaiting_user', 'handoff_closing')
            """,
            (
                "The local service restarted while this browser session was active. "
                "Start the application or sign-in handoff again.",
                now_iso(),
            ),
        )
