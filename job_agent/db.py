from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH, DOCS_DIR, GENERATED_DIR


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in (DATA_DIR, DOCS_DIR, GENERATED_DIR):
        path.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              path TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'source',
              content TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              sha256 TEXT NOT NULL DEFAULT '',
              extractor TEXT NOT NULL DEFAULT '',
              ingest_status TEXT NOT NULL DEFAULT 'ready',
              ingest_error TEXT NOT NULL DEFAULT '',
              size_bytes INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}',
              source TEXT NOT NULL DEFAULT 'folder',
              review_status TEXT NOT NULL DEFAULT 'approved',
              extraction_confidence REAL NOT NULL DEFAULT 0,
              classification_confidence REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT '',
              archived_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_profiles (
              id INTEGER PRIMARY KEY CHECK(id = 1),
              profile_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              company TEXT NOT NULL,
              url TEXT NOT NULL UNIQUE,
              description TEXT NOT NULL,
              location TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'manual',
              status TEXT NOT NULL DEFAULT 'new',
              score INTEGER NOT NULL DEFAULT 0,
              posted_at TEXT,
              external_id TEXT NOT NULL DEFAULT '',
              source_key TEXT NOT NULL DEFAULT '',
              fingerprint TEXT NOT NULL DEFAULT '',
              apply_url TEXT NOT NULL DEFAULT '',
              workplace_type TEXT NOT NULL DEFAULT '',
              match_reasons TEXT NOT NULL DEFAULT '[]',
              metadata TEXT NOT NULL DEFAULT '{}',
              discovered_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL DEFAULT '',
              description_fetched_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_source_states (
              source_url TEXT PRIMARY KEY,
              source_kind TEXT NOT NULL,
              cursor TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'idle',
              pages_scanned INTEGER NOT NULL DEFAULT 0,
              jobs_seen INTEGER NOT NULL DEFAULT 0,
              scan_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              metadata TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_scanned_at TEXT NOT NULL DEFAULT '',
              last_success_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS pipeline_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER NOT NULL UNIQUE,
              application_id INTEGER,
              status TEXT NOT NULL DEFAULT 'queued',
              stage TEXT NOT NULL DEFAULT 'discovered',
              message TEXT NOT NULL DEFAULT '',
              policy_json TEXT NOT NULL DEFAULT '{}',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(job_id) REFERENCES jobs(id),
              FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS pipeline_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              pipeline_item_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              stage TEXT NOT NULL,
              message TEXT NOT NULL,
              meta TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(pipeline_item_id) REFERENCES pipeline_items(id)
            );

            CREATE TABLE IF NOT EXISTS applications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id INTEGER NOT NULL,
              mode TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'drafted',
              resume_tex_path TEXT NOT NULL DEFAULT '',
              resume_pdf_path TEXT NOT NULL DEFAULT '',
              resume_compile_status TEXT NOT NULL DEFAULT 'pending',
              resume_compile_engine TEXT NOT NULL DEFAULT '',
              resume_compile_message TEXT NOT NULL DEFAULT '',
              resume_compile_log TEXT NOT NULL DEFAULT '',
              resume_pdf_pages INTEGER NOT NULL DEFAULT 0,
              resume_pdf_bytes INTEGER NOT NULL DEFAULT 0,
              resume_compiled_at TEXT NOT NULL DEFAULT '',
              current_writing_version_id INTEGER,
              approved_writing_version_id INTEGER,
              writing_status TEXT NOT NULL DEFAULT 'draft',
              writing_message TEXT NOT NULL DEFAULT '',
              cover_letter TEXT NOT NULL DEFAULT '',
              statements TEXT NOT NULL DEFAULT '[]',
              email_subject TEXT NOT NULL DEFAULT '',
              email_body TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS writing_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              application_id INTEGER NOT NULL,
              version INTEGER NOT NULL,
              origin TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft',
              content_json TEXT NOT NULL,
              evidence_json TEXT NOT NULL DEFAULT '[]',
              validation_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              approved_at TEXT NOT NULL DEFAULT '',
              UNIQUE(application_id, version),
              FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS writing_tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              application_id INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              request_json TEXT NOT NULL DEFAULT '{}',
              result_json TEXT NOT NULL DEFAULT '{}',
              task_dir TEXT NOT NULL DEFAULT '',
              output_path TEXT NOT NULL DEFAULT '',
              message TEXT NOT NULL DEFAULT '',
              log TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS browser_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_key TEXT NOT NULL UNIQUE,
              adapter TEXT NOT NULL,
              hostname TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'new',
              target_url TEXT NOT NULL DEFAULT '',
              active_task_id INTEGER,
              message TEXT NOT NULL DEFAULT '',
              last_verified_at TEXT NOT NULL DEFAULT '',
              last_used_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS application_tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              application_id INTEGER NOT NULL,
              browser_session_id INTEGER,
              adapter TEXT NOT NULL DEFAULT '',
              target_url TEXT NOT NULL,
              resume_url TEXT NOT NULL DEFAULT '',
              mode TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued',
              current_step TEXT NOT NULL DEFAULT 'queued',
              message TEXT NOT NULL DEFAULT '',
              checkpoint_kind TEXT NOT NULL DEFAULT '',
              checkpoint_json TEXT NOT NULL DEFAULT '{}',
              answers_json TEXT NOT NULL DEFAULT '{}',
              form_snapshot_json TEXT NOT NULL DEFAULT '[]',
              result_json TEXT NOT NULL DEFAULT '{}',
              screenshots_json TEXT NOT NULL DEFAULT '[]',
              artifact_dir TEXT NOT NULL DEFAULT '',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL DEFAULT '',
              retry_category TEXT NOT NULL DEFAULT '',
              retry_reason TEXT NOT NULL DEFAULT '',
              retry_exhausted INTEGER NOT NULL DEFAULT 0,
              final_submit_approved INTEGER NOT NULL DEFAULT 0,
              submit_started_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(application_id) REFERENCES applications(id),
              FOREIGN KEY(browser_session_id) REFERENCES browser_sessions(id)
            );

            CREATE TABLE IF NOT EXISTS application_task_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              level TEXT NOT NULL DEFAULT 'info',
              step TEXT NOT NULL,
              message TEXT NOT NULL,
              meta TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES application_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS browser_diagnostic_bundles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              adapter TEXT NOT NULL,
              hostname TEXT NOT NULL DEFAULT '',
              outcome_status TEXT NOT NULL,
              category TEXT NOT NULL,
              severity TEXT NOT NULL,
              retryable INTEGER NOT NULL DEFAULT 0,
              recommendation TEXT NOT NULL,
              artifact_path TEXT NOT NULL DEFAULT '',
              summary_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES application_tasks(id)
            );

            CREATE TABLE IF NOT EXISTS adapter_health (
              adapter TEXT NOT NULL,
              hostname TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              submitted INTEGER NOT NULL DEFAULT 0,
              checkpoints INTEGER NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0,
              manual_submissions INTEGER NOT NULL DEFAULT 0,
              last_outcome TEXT NOT NULL DEFAULT '',
              last_category TEXT NOT NULL DEFAULT '',
              last_message TEXT NOT NULL DEFAULT '',
              category_counts_json TEXT NOT NULL DEFAULT '{}',
              last_attempt_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(adapter, hostname)
            );

            CREATE TABLE IF NOT EXISTS adapter_circuit_breakers (
              adapter TEXT NOT NULL,
              hostname TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'closed',
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              opened_at TEXT NOT NULL DEFAULT '',
              retry_after TEXT NOT NULL DEFAULT '',
              last_category TEXT NOT NULL DEFAULT '',
              last_message TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(adapter, hostname)
            );

            CREATE TABLE IF NOT EXISTS ats_adapter_states (
              adapter TEXT NOT NULL,
              hostname TEXT NOT NULL,
              adapter_version TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              consecutive_drift INTEGER NOT NULL DEFAULT 0,
              total_drift INTEGER NOT NULL DEFAULT 0,
              last_category TEXT NOT NULL DEFAULT '',
              last_message TEXT NOT NULL DEFAULT '',
              last_signature_json TEXT NOT NULL DEFAULT '{}',
              last_diagnostic_id INTEGER,
              quarantined_at TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(adapter, hostname),
              FOREIGN KEY(last_diagnostic_id) REFERENCES browser_diagnostic_bundles(id)
            );

            CREATE TABLE IF NOT EXISTS ats_replay_fixtures (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL,
              diagnostic_id INTEGER NOT NULL UNIQUE,
              adapter TEXT NOT NULL,
              hostname TEXT NOT NULL,
              adapter_version TEXT NOT NULL,
              category TEXT NOT NULL,
              signature_json TEXT NOT NULL DEFAULT '{}',
              artifact_path TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES application_tasks(id),
              FOREIGN KEY(diagnostic_id) REFERENCES browser_diagnostic_bundles(id)
            );

            CREATE TABLE IF NOT EXISTS answer_rules (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              question TEXT NOT NULL UNIQUE,
              answer TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'general',
              risky INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contacts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              company TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              confidence INTEGER NOT NULL DEFAULT 0,
              verification_status TEXT NOT NULL DEFAULT 'unverified',
              email_kind TEXT NOT NULL DEFAULT 'manual',
              relevance_score INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}',
              discovery_run_id INTEGER,
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT '',
              discovered_at TEXT NOT NULL DEFAULT '',
              verified_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS contact_discovery_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              application_id INTEGER NOT NULL,
              company TEXT NOT NULL,
              seed_url TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'running',
              pages_scanned INTEGER NOT NULL DEFAULT 0,
              candidates_found INTEGER NOT NULL DEFAULT 0,
              contacts_added INTEGER NOT NULL DEFAULT 0,
              contacts_updated INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              log TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL,
              completed_at TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS outreach_threads (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              application_id INTEGER NOT NULL,
              contact_id INTEGER NOT NULL,
              writing_version_id INTEGER NOT NULL,
              active_revision_id INTEGER,
              approved_revision_id INTEGER,
              status TEXT NOT NULL DEFAULT 'draft',
              recipient_email TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT NOT NULL DEFAULT '',
              approved_at TEXT NOT NULL DEFAULT '',
              queued_at TEXT NOT NULL DEFAULT '',
              sent_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(application_id, contact_id),
              FOREIGN KEY(application_id) REFERENCES applications(id),
              FOREIGN KEY(contact_id) REFERENCES contacts(id),
              FOREIGN KEY(writing_version_id) REFERENCES writing_versions(id)
            );

            CREATE TABLE IF NOT EXISTS outreach_revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              thread_id INTEGER NOT NULL,
              version INTEGER NOT NULL,
              subject TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(thread_id, version),
              FOREIGN KEY(thread_id) REFERENCES outreach_threads(id)
            );

            CREATE TABLE IF NOT EXISTS approval_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dedupe_key TEXT NOT NULL UNIQUE,
              kind TEXT NOT NULL,
              source_type TEXT NOT NULL,
              source_id INTEGER NOT NULL,
              application_id INTEGER,
              priority INTEGER NOT NULL DEFAULT 50,
              title TEXT NOT NULL,
              summary TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              actions_json TEXT NOT NULL DEFAULT '[]',
              payload_json TEXT NOT NULL DEFAULT '{}',
              source_updated_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              resolved_at TEXT NOT NULL DEFAULT '',
              resolution TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS approval_decisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              approval_item_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              FOREIGN KEY(approval_item_id) REFERENCES approval_items(id)
            );

            CREATE TABLE IF NOT EXISTS notification_deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              approval_item_id INTEGER NOT NULL,
              dedupe_key TEXT NOT NULL UNIQUE,
              channel TEXT NOT NULL DEFAULT 'macos',
              status TEXT NOT NULL DEFAULT 'queued',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              message TEXT NOT NULL DEFAULT '',
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              sent_at TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(approval_item_id) REFERENCES approval_items(id)
            );

            CREATE TABLE IF NOT EXISTS readiness_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              status TEXT NOT NULL,
              score INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              level TEXT NOT NULL DEFAULT 'info',
              message TEXT NOT NULL,
              meta TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            """
        )
        document_columns = {
            "sha256": "TEXT NOT NULL DEFAULT ''",
            "extractor": "TEXT NOT NULL DEFAULT ''",
            "ingest_status": "TEXT NOT NULL DEFAULT 'ready'",
            "ingest_error": "TEXT NOT NULL DEFAULT ''",
            "size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
            "source": "TEXT NOT NULL DEFAULT 'folder'",
            "review_status": "TEXT NOT NULL DEFAULT 'approved'",
            "extraction_confidence": "REAL NOT NULL DEFAULT 0",
            "classification_confidence": "REAL NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "archived_at": "TEXT NOT NULL DEFAULT ''",
        }
        existing_columns = {item["name"] for item in conn.execute("PRAGMA table_info(documents)").fetchall()}
        for name, definition in document_columns.items():
            if name not in existing_columns:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
        conn.execute(
            "UPDATE documents SET created_at = updated_at WHERE created_at = ''"
        )
        conn.execute(
            """
            UPDATE documents
            SET extraction_confidence = CASE
              WHEN extractor IN ('utf-8-text', 'docx-ooxml') THEN 1.0
              WHEN extractor = 'pypdf' THEN 0.98
              ELSE extraction_confidence
            END
            WHERE extraction_confidence = 0
            """
        )
        conn.execute(
            """
            UPDATE documents
            SET classification_confidence =
              CASE WHEN kind = 'source' THEN 0.35 ELSE 0.95 END
            WHERE classification_confidence = 0
            """
        )
        job_columns = {
            "external_id": "TEXT NOT NULL DEFAULT ''",
            "source_key": "TEXT NOT NULL DEFAULT ''",
            "fingerprint": "TEXT NOT NULL DEFAULT ''",
            "apply_url": "TEXT NOT NULL DEFAULT ''",
            "workplace_type": "TEXT NOT NULL DEFAULT ''",
            "match_reasons": "TEXT NOT NULL DEFAULT '[]'",
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
            "last_seen_at": "TEXT NOT NULL DEFAULT ''",
            "description_fetched_at": "TEXT NOT NULL DEFAULT ''",
        }
        existing_job_columns = {item["name"] for item in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, definition in job_columns.items():
            if name not in existing_job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_key "
            "ON jobs(source_key) WHERE source_key <> ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_source_states_status "
            "ON job_source_states(status, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_items_status "
            "ON pipeline_items(status, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_events_item "
            "ON pipeline_events(pipeline_item_id, created_at)"
        )
        application_columns = {
            "resume_compile_status": "TEXT NOT NULL DEFAULT 'pending'",
            "resume_compile_engine": "TEXT NOT NULL DEFAULT ''",
            "resume_compile_message": "TEXT NOT NULL DEFAULT ''",
            "resume_compile_log": "TEXT NOT NULL DEFAULT ''",
            "resume_pdf_pages": "INTEGER NOT NULL DEFAULT 0",
            "resume_pdf_bytes": "INTEGER NOT NULL DEFAULT 0",
            "resume_compiled_at": "TEXT NOT NULL DEFAULT ''",
            "current_writing_version_id": "INTEGER",
            "approved_writing_version_id": "INTEGER",
            "writing_status": "TEXT NOT NULL DEFAULT 'draft'",
            "writing_message": "TEXT NOT NULL DEFAULT ''",
        }
        existing_application_columns = {
            item["name"] for item in conn.execute("PRAGMA table_info(applications)").fetchall()
        }
        for name, definition in application_columns.items():
            if name not in existing_application_columns:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_writing_tasks_status "
            "ON writing_tasks(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_tasks_status "
            "ON application_tasks(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_tasks_application "
            "ON application_tasks(application_id, created_at)"
        )
        application_task_columns = {
            item["name"]
            for item in conn.execute("PRAGMA table_info(application_tasks)").fetchall()
        }
        if "resume_url" not in application_task_columns:
            conn.execute(
                "ALTER TABLE application_tasks ADD COLUMN resume_url TEXT NOT NULL DEFAULT ''"
            )
        if "browser_session_id" not in application_task_columns:
            conn.execute(
                "ALTER TABLE application_tasks ADD COLUMN browser_session_id INTEGER"
            )
        recovery_columns = {
            "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
            "retry_category": "TEXT NOT NULL DEFAULT ''",
            "retry_reason": "TEXT NOT NULL DEFAULT ''",
            "retry_exhausted": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in recovery_columns.items():
            if name not in application_task_columns:
                conn.execute(
                    f"ALTER TABLE application_tasks ADD COLUMN {name} {definition}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_tasks_session "
            "ON application_tasks(browser_session_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_tasks_retry "
            "ON application_tasks(status, next_attempt_at, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browser_sessions_status "
            "ON browser_sessions(status, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_application_task_events_task "
            "ON application_task_events(task_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browser_diagnostics_task "
            "ON browser_diagnostic_bundles(task_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browser_diagnostics_category "
            "ON browser_diagnostic_bundles(category, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_adapter_health_updated "
            "ON adapter_health(updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_adapter_circuits_status "
            "ON adapter_circuit_breakers(status, retry_after)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ats_adapter_states_status "
            "ON ats_adapter_states(status, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ats_replay_adapter "
            "ON ats_replay_fixtures(adapter, hostname, created_at DESC)"
        )
        contact_columns = {
            "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
            "email_kind": "TEXT NOT NULL DEFAULT 'manual'",
            "relevance_score": "INTEGER NOT NULL DEFAULT 0",
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
            "discovery_run_id": "INTEGER",
            "notes": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "discovered_at": "TEXT NOT NULL DEFAULT ''",
            "verified_at": "TEXT NOT NULL DEFAULT ''",
        }
        existing_contact_columns = {
            item["name"] for item in conn.execute("PRAGMA table_info(contacts)").fetchall()
        }
        for name, definition in contact_columns.items():
            if name not in existing_contact_columns:
                conn.execute(f"ALTER TABLE contacts ADD COLUMN {name} {definition}")
        conn.execute(
            """
            UPDATE contacts SET verification_status = 'manual'
            WHERE email_kind = 'manual' AND verification_status = 'unverified'
              AND discovery_run_id IS NULL
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_discovery_runs_application "
            "ON contact_discovery_runs(application_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outreach_threads_status "
            "ON outreach_threads(status, queued_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_items_status "
            "ON approval_items(status, priority DESC, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_decisions_item "
            "ON approval_decisions(approval_item_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_deliveries_status "
            "ON notification_deliveries(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readiness_runs_created "
            "ON readiness_runs(created_at DESC)"
        )
        defaults = {
            "mode": "review",
            "role_keywords": "software engineer, developer, full stack",
            "locations": "remote, new york, nyc",
            "posted_age_mode": "days",
            "posted_within_hours": "24",
            "posted_within_days": "14",
            "include_unknown_posted_at": "true",
            "daily_application_limit": "10",
            "daily_email_limit": "15",
            "scan_interval_minutes": "0",
            "max_jobs_per_source": "80",
            "target_companies": "",
            "career_urls": "",
            "email_mode": "approval",
            "contact_discovery_max_pages": "8",
            "browser_headless": "true",
            "browser_submit_enabled": "false",
            "browser_allow_sensitive_answers": "false",
            "browser_login_timeout_minutes": "15",
            "browser_retry_enabled": "true",
            "browser_retry_max_attempts": "3",
            "browser_retry_base_delay_seconds": "60",
            "browser_retry_max_delay_seconds": "900",
            "browser_circuit_failure_threshold": "3",
            "browser_circuit_cooldown_minutes": "30",
            "browser_adapter_drift_threshold": "2",
            "pipeline_enabled": "false",
            "pipeline_min_score": "75",
            "pipeline_auto_write": "true",
            "pipeline_auto_approve": "false",
            "pipeline_auto_apply": "true",
            "notifications_enabled": "true",
            "notification_quiet_start": "22:00",
            "notification_quiet_end": "08:00",
            "document_review_mode": "false",
            "document_scan_interval_seconds": "15",
            "setup_completed_at": "",
            "smtp_verified_at": "",
            "smtp_verification_status": "",
            "smtp_verification_message": "",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        found = conn.execute(query, params).fetchone()
        return dict(found) if found else None


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(query, params)
        return int(cur.lastrowid or 0)


def setting(key: str, default: str = "") -> str:
    found = row("SELECT value FROM settings WHERE key = ?", (key,))
    return str(found["value"]) if found else default


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def all_settings() -> dict[str, str]:
    return {item["key"]: item["value"] for item in rows("SELECT key, value FROM settings")}


def log(message: str, level: str = "info", meta: dict[str, Any] | None = None) -> None:
    execute(
        "INSERT INTO events(level, message, meta, created_at) VALUES(?, ?, ?, ?)",
        (level, message, json.dumps(meta or {}), now_iso()),
    )


def db_info() -> dict[str, str]:
    return {
        "database": str(DB_PATH),
        "docs": str(DOCS_DIR),
        "generated": str(GENERATED_DIR),
    }
