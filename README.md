# ApplyForMe Local Agent

Local-first job application automation scaffold for a Codex-operated workflow.

## What works now

- Local browser dashboard at `http://127.0.0.1:8787`.
- SQLite tracker in `data/applyforme.sqlite3`.
- PDF, DOCX, and text document ingestion from `docs/`.
- A source-grounded structured candidate profile with contact, skills, education, certifications, and evidence.
- Manual job entry and enriched career-page scanning with Greenhouse and Lever adapters.
- Role, company, location, and posting-age filters with canonical URL and ATS-ID deduplication.
- Tailored LaTeX resume generation with validated PDF output for each job.
- Dashboard PDF preview, LaTeX download, recompile controls, and compiler diagnostics.
- Evidence-grounded writing versions for resume content, cover letters, statements, and outreach.
- A local Codex writing queue that uses saved ChatGPT authentication instead of an OpenAI API key.
- Application package tracking with review, approval, and submitted states.
- Saved answer rules for repeated application-form questions.
- Persistent background browser tasks with Greenhouse and Lever application adapters.
- Resume upload, plain-English task events, screenshots, and resumable question checkpoints.
- CAPTCHA, login, sensitive-question, unknown-field, final-review, and uncertain-submission stops.
- Contact-specific outreach revisions, explicit approval, retry controls, and SMTP daily limits.
- Bounded public company-page contact discovery with role ranking, source provenance, and verification gates.
- Plain-English activity log.

## What is intentionally guarded

- Browser submission supports Greenhouse and Lever; other application systems stop at a manual checkpoint.
- CAPTCHA challenges are never bypassed, and login-required forms stop for manual intervention.
- Review and assisted modes always stop before the final submit action. Rules-autonomous final submission requires the separate **Final browser submission** setting.
- Sensitive saved answers pause for confirmation unless explicitly enabled in settings.
- PDF compilation reports a clear blocked state when no supported local TeX engine is available.
- Codex writing requires an explicit local queue action and a ChatGPT-authenticated Codex CLI session.
- Social-network scraping is disabled; inferred email patterns cannot be used until explicitly verified.

## Run

Install the system tools used for reliable compilation and visual PDF checks on macOS:

```bash
brew install tectonic poppler
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

## Source documents

Add readable source files to `docs/`, then click **Ingest docs** in the dashboard.

Supported source formats:

- `.txt`
- `.md`
- `.tex`
- `.csv`
- `.pdf` with embedded text
- `.docx`

Files are classified from their names, tracked by content hash, and re-ingested only when changed. Removed files are also removed from the local candidate profile. Image-only PDFs are reported as requiring OCR rather than being silently ignored.

## Job sources

Add company career pages, Greenhouse boards, or Lever sites under **Settings > Career URLs**. Scans fetch complete descriptions, normalize location and posting dates where the source exposes them, and retain matching reasons with each job. Greenhouse and Lever public feeds do not require API credentials for discovery.

The posting-age filter is applied when a source supplies a date. Jobs without a source date remain visible and are marked accordingly rather than being silently discarded.

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
