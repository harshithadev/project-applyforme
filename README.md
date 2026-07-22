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
- Application package tracking with review, approval, and submitted states.
- Saved answer rules for repeated application-form questions.
- Email sending limits and SMTP-backed sending endpoint.
- Plain-English activity log.

## What is intentionally guarded

- Browser submission is queued until Playwright and site-specific adapters are installed.
- PDF compilation reports a clear blocked state when no supported local TeX engine is available.
- AI writing is designed for active Codex/ChatGPT operation in v1, so the local backend does not require OpenAI API billing.

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

Run the core ingestion and application lifecycle tests with:

```bash
npm test
```

Run the discovery adapter and filter tests independently with:

```bash
npm run test:jobs
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

## CLI

```bash
npm run init
npm run ingest-docs
npm run scan-jobs
python3 -m job_agent.cli draft <job_id>
```
