# ApplyForMe Local Agent

Local-first job application automation scaffold for a Codex-operated workflow.

## What works now

- Local browser dashboard at `http://127.0.0.1:8787`.
- SQLite tracker in `data/applyforme.sqlite3`.
- PDF, DOCX, and text document ingestion from `docs/`.
- Secure website uploads, automatic folder reconciliation, duplicate detection, and local scanned-PDF OCR.
- Document review, classification, extraction confidence, archive, restore, and removal controls.
- A source-grounded structured candidate profile with contact, skills, education, certifications, and evidence.
- Manual job entry and enriched career-page scanning with Greenhouse, Lever, Ashby, SmartRecruiters, and Workday adapters.
- Persistent per-source cursors, pagination status, scan counts, and errors in the dashboard.
- Role, company, location, and posting-age filters with canonical URL and ATS-ID deduplication.
- A durable score-gated pipeline from discovery through tailoring, review, and guarded browser automation.
- Per-job pipeline history, retries, skips, daily intake limits, and crash recovery.
- A per-user macOS login service with restart controls and local stdout/stderr logs.
- A persisted setup preflight that gates tailoring, review automation, outreach, and rules-autonomous modes.
- Tailored LaTeX resume generation with validated PDF output for each job.
- Dashboard PDF preview, LaTeX download, recompile controls, and compiler diagnostics.
- Evidence-grounded writing versions for resume content, cover letters, statements, and outreach.
- A local Codex writing queue that uses saved ChatGPT authentication instead of an OpenAI API key.
- Application package tracking with review, approval, and submitted states.
- A unified approval inbox with immutable decisions and deduplicated macOS notifications.
- Saved answer rules for repeated application-form questions.
- Persistent background browser tasks with Greenhouse, Lever, Ashby, SmartRecruiters, and Workday application adapters.
- Guarded multi-step form advancement for SmartRecruiters, Workday, and compatible Ashby flows.
- Resume upload, plain-English task events, screenshots, and resumable question checkpoints.
- CAPTCHA, login, sensitive-question, unknown-field, final-review, and uncertain-submission stops.
- Contact-specific outreach revisions, explicit approval, retry controls, and SMTP daily limits.
- Bounded public company-page contact discovery with role ranking, source provenance, and verification gates.
- Plain-English activity log.

## What is intentionally guarded

- Browser submission supports Greenhouse, Lever, Ashby, SmartRecruiters, and public Workday manual-application flows; other application systems stop at a manual checkpoint.
- Workday applications that require an account stop at a login checkpoint and are not submitted automatically.
- CAPTCHA challenges are never bypassed, and login-required forms stop for manual intervention.
- Review and assisted modes always stop before the final submit action. Rules-autonomous final submission requires the separate **Final browser submission** setting.
- Sensitive saved answers pause for confirmation unless explicitly enabled in settings.
- PDF compilation reports a clear blocked state when no supported local TeX engine is available.
- Codex writing requires an explicit local queue action and a ChatGPT-authenticated Codex CLI session.
- Social-network scraping is disabled; inferred email patterns cannot be used until explicitly verified.

## Run

Install the system tools used for reliable compilation and visual PDF checks on macOS:

```bash
brew install tectonic poppler tesseract
```

Then install the Python dependencies and start the app:

```bash
npm run setup
npm run dev
```

Then open:

```text
http://127.0.0.1:8787
```

## macOS background service

Install and immediately start the per-user login service:

```bash
npm run service:install
```

It installs `~/Library/LaunchAgents/com.applyforme.local-agent.plist` without root access. The service starts after login, restarts after crashes, runs from this repository, and continues serving the dashboard at `http://127.0.0.1:8787`. Do not run `npm run dev` at the same time because both processes use port 8787.

Manage it with:

```bash
npm run service:status
npm run service:restart
npm run service:uninstall
```

Service output is stored in `data/logs/launch-agent.out.log` and `data/logs/launch-agent.err.log`. The ignored `.env` file is loaded by the service runner for SMTP configuration; keep it readable only by your account:

```bash
chmod 600 .env
```

## Setup and readiness

The **Setup** view evaluates the running system instead of relying on a static onboarding checklist. It checks the login service, ChatGPT-authenticated Codex session, LaTeX compiler, approved evidence, job discovery input, Playwright, document watcher, OCR, SMTP verification, pipeline policy, and autonomous safety switches.

Readiness is reported separately for:

- Tailored documents
- Review automation
- Verified outreach
- Rules-autonomous operation

Each explicit **Run preflight** action stores a timestamped snapshot in SQLite. **Complete setup** remains blocked until review automation has approved evidence, a job source or saved job, and every required local capability. Later configuration changes can lower current readiness without deleting the historical completion record.

The email connection test performs TLS and login only. It does not send a message, store SMTP credentials, or expose secrets through the dashboard. SMTP credentials continue to come from the ignored `.env` file or process environment.

Run the blocked-to-ready transitions, SMTP login-only verification, persisted history, completion guard, and autonomous safety-switch test with:

```bash
npm run test:readiness
```

## Source documents

Add source files from the **Documents** view or place them directly in `docs/`. The login service watches the folder and reconciles additions, changes, and removals automatically. **Ingest docs** remains available for an immediate manual scan.

Supported source formats:

- `.txt`
- `.md`
- `.tex`
- `.csv`
- `.pdf` with embedded text or scanned page images
- `.docx`

Uploads are restricted by extension, signature, filename, file count, and size before being written atomically to local storage. Files are classified from their names, tracked by content hash, and re-ingested only when changed. Duplicate content is blocked even when the filename differs.

Image-only PDFs are rendered locally with Poppler and read by Tesseract. OCR confidence, extraction method, page limits, warnings, and source provenance remain attached to the document. No document content is sent to a paid OCR or external AI service.

Enable **Settings > Document review** to keep newly extracted evidence out of the candidate profile until it is approved. Documents can be renamed, manually classified, previewed, archived, restored, or permanently removed. New documents, review requests, duplicates, and extraction failures also appear in the Approval Inbox and use its notification policy.

Run the upload, OCR, review, duplicate, lifecycle, profile-rebuild, and folder-watcher test with:

```bash
npm run test:documents
```

## Job sources

Add company career pages or public Greenhouse, Lever, Ashby, SmartRecruiters, and Workday career-site URLs under **Settings > Career URLs**. Scans fetch complete descriptions, normalize location and posting dates where the source exposes them, and retain matching reasons with each job. These public discovery feeds do not require API credentials.

Ashby, SmartRecruiters, and Workday scans paginate up to **Max jobs per source** per run. The next offset is stored in SQLite and resumed on the next scan; the **Source scans** panel shows progress, errors, and when a full cycle completes.

The posting-age filter is applied when a source supplies a date. Jobs without a source date remain visible and are marked accordingly rather than being silently discarded.

## Application pipeline

Enable the pipeline under **Settings > Application pipeline**. New jobs at or above the configured score are admitted up to the daily application limit. Each item retains the policy that admitted it, its current stage, attempt count, errors, and transition history.

With automatic Codex tailoring enabled, the pipeline queues grounded resume, cover-letter, statement, and outreach generation through the ChatGPT-authenticated Codex CLI. It then validates the evidence references and compiles the LaTeX resume. Review mode waits for approval before browser automation. Assisted-autonomous mode can fill forms but still stops at final review. Rules-autonomous approval and final browser submission each require their own explicit settings.

Blocked and failed items remain paused until **Retry** is selected. Skipping an item cancels queued writing and browser work when it can be stopped safely; running or uncertain submissions must be resolved first.

## Approval inbox

The **Approvals** view combines tailored-package review, browser checkpoints, outreach drafts, and failed or blocked pipeline work. Every item is tied to the exact writing version, outreach revision, or task checkpoint that produced it. If the source changes, the old item is closed instead of being reused for newer content.

Actions delegate to the same guarded workflows used in the detailed views. Unknown and sensitive form questions require complete answers, final submission has a separate confirmation, and uncertain submissions must be verified before they can be marked submitted. Optional decision notes and action results remain in SQLite.

Native macOS notifications are enabled by default and sent once per inbox item. Configure quiet hours or disable notifications under **Settings**. Use **Test notification** in the Approval Inbox to verify local notification delivery.

Run the inbox, decision-history, answer-validation, notification-deduplication, and quiet-hours test with:

```bash
npm run test:approvals
```

## Codex writer

Sign the local Codex CLI in with ChatGPT before using **Generate with Codex**:

```bash
codex login
codex login status
```

The worker reuses that saved ChatGPT login. It refuses other authentication modes, removes API keys and application secrets from the subprocess environment, and runs each request in an isolated nested Git repository with a read-only sandbox. Each response must satisfy a JSON schema and pass local evidence validation before it can replace the active draft.

Codex-generated drafts use your ChatGPT/Codex plan allowance and remain subject to its limits. The dashboard does not convert a ChatGPT subscription into general OpenAI API access.

Run the core ingestion and application lifecycle tests with:

```bash
npm test
```

Run the discovery adapter and filter tests independently with:

```bash
npm run test:jobs
```

Run the durable pipeline and guarded end-to-end transition test with:

```bash
npm run test:pipeline
```

Run the persistent task lifecycle and real local Chromium adapter tests with:

```bash
npm run test:applications
npm run test:browser
```

Run the writing version, validation, rollback, and isolated queue tests with:

```bash
npm run test:writing
```

Run an optional live Codex generation smoke test, which uses ChatGPT/Codex plan allowance:

```bash
npm run test:codex-live
```

Run the compiler success and failure-path test independently with:

```bash
npm run test:latex
```

## Email setup

Copy `.env.example` values into your shell environment before starting the server if you want sending enabled.

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=you@example.com
export SMTP_PASSWORD=app-password
export EMAIL_FROM=you@example.com
npm run dev
```

Add a contact in **Outreach**, select a matching application, and create a personalized draft. Editing creates an immutable revision and resets approval. In approval mode, only the current approved revision can enter the delivery queue. Delivery failures require an explicit retry, server interruptions are marked uncertain, and application writing changes require a new revision before sending.

Run the contact, approval, delivery, retry, stale-writing, and daily-limit tests with:

```bash
npm run test:outreach
```

## Contact discovery

From **Outreach**, select an application and optionally enter the public company website before choosing **Discover public contacts**. Discovery stays on the same website, respects its robots policy, follows only company/team/contact-style links, and stops at the configured page limit.

Published addresses retain their source page. Name-based `first.last` inferences are labeled unverified and are blocked from outreach until you explicitly verify them. Rejected contacts stay rejected across later scans. LinkedIn and other social-network pages are not scanned.

Run the public-page parsing, ranking, provenance, deduplication, and verification tests with:

```bash
npm run test:contacts
```

## CLI

```bash
npm run init
npm run ingest-docs
npm run scan-jobs
python3 -m job_agent.cli draft <job_id>
python3 -m job_agent.cli write <application_id>
python3 -m job_agent.cli process-writing
python3 -m job_agent.cli process-outreach
python3 -m job_agent.cli apply <application_id>
python3 -m job_agent.cli process-application
python3 -m job_agent.cli discover-contacts <application_id> --url https://company.example
```
