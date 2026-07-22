from __future__ import annotations

import argparse

from . import applications, jobs, profile
from .db import init_db, rows


def main() -> None:
    parser = argparse.ArgumentParser(description="ApplyForMe local job agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("ingest-docs")
    sub.add_parser("scan-jobs")
    draft = sub.add_parser("draft")
    draft.add_argument("job_id", type=int)
    sub.add_parser("state")
    args = parser.parse_args()
    init_db()
    if args.command == "init":
        print("Initialized local database and folders.")
    elif args.command == "ingest-docs":
        print(profile.ingest_docs())
    elif args.command == "scan-jobs":
        print(jobs.discover_jobs())
    elif args.command == "draft":
        app = applications.draft_application(args.job_id)
        print(f"Drafted application {app.get('id')}: {app.get('resume_tex_path')}")
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
