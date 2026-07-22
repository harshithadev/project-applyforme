# ApplyForMe Local Agent

Local-first job application automation scaffold for a Codex-operated workflow.

## What works now

- Local browser dashboard at `http://127.0.0.1:8787`.
- SQLite tracker in `data/applyforme.sqlite3`.
- PDF, DOCX, and text document ingestion from `docs/`.
- A source-grounded structured candidate profile with contact, skills, education, certifications, and evidence.
- Manual job entry and configurable career-page scanning.
- Tailored LaTeX resume source generation for each job.
- Application package tracking with review, approval, and submitted states.
- Saved answer rules for repeated application-form questions.
- Email sending limits and SMTP-backed sending endpoint.
- Plain-English activity log.

## What is intentionally guarded

- Browser submission is queued until Playwright and site-specific adapters are installed.
- PDF compilation needs a local TeX engine such as `tectonic`, `pdflatex`, `xelatex`, or `lualatex`.
- AI writing is designed for active Codex/ChatGPT operation in v1, so the local backend does not require OpenAI API billing.

## Run

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

Run the core ingestion and application lifecycle tests with:

```bash
npm test
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
