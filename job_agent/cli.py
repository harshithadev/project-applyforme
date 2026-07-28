from __future__ import annotations

import argparse

from . import applications, automation, contact_discovery, jobs, outreach, profile, service, writing
from .db import init_db, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="ApplyForMe local job agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("ingest-docs")
    sub.add_parser("scan-jobs")
    draft = sub.add_parser("draft")
    draft.add_argument("job_id", type=int)
    write = sub.add_parser("write")
    write.add_argument("application_id", type=int)
    sub.add_parser("process-writing")
    sub.add_parser("process-outreach")
    apply = sub.add_parser("apply")
    apply.add_argument("application_id", type=int)
    sub.add_parser("process-application")
    sub.add_parser("service-install")
    sub.add_parser("service-status")
    sub.add_parser("service-restart")
    sub.add_parser("service-uninstall")
    discover_contacts = sub.add_parser("discover-contacts")
    discover_contacts.add_argument("application_id", type=int)
    discover_contacts.add_argument("--url", default="")
    sub.add_parser("state")
    args = parser.parse_args()
    init_db()
    if args.command == "init":
        print("Initialized local database and folders.")
    elif args.command == "ingest-docs":
        print(profile.ingest_docs())
    elif args.command == "scan-jobs":
        print(jobs.discover_jobs(force=True))
    elif args.command == "draft":
        app = applications.draft_application(args.job_id)
        print(f"Drafted application {app.get('id')}: {app.get('resume_tex_path')}")
    elif args.command == "write":
        print(writing.queue_codex_draft(args.application_id))
    elif args.command == "process-writing":
        print(writing.process_next_task() or {"status": "idle"})
    elif args.command == "process-outreach":
        print(outreach.process_next() or {"status": "idle"})
    elif args.command == "apply":
        print(automation.apply_application(args.application_id))
    elif args.command == "process-application":
        print(automation.process_next_task() or {"status": "idle"})
    elif args.command == "service-install":
        print(service.install())
    elif args.command == "service-status":
        print(service.status())
    elif args.command == "service-restart":
        print(service.restart())
    elif args.command == "service-uninstall":
        print(service.uninstall())
    elif args.command == "discover-contacts":
        print(contact_discovery.discover_for_application(args.application_id, args.url))
    elif args.command == "state":
        print(
            {
                "jobs": len(rows("SELECT id FROM jobs")),
                "applications": len(rows("SELECT id FROM applications")),
                "documents": len(rows("SELECT id FROM documents")),
            }
        )


if __name__ == "__main__":
    main()
