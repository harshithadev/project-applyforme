from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import GENERATED_DIR, LATEX_ENGINES
from .db import log


@dataclass(frozen=True)
class CompilationResult:
    status: str
    engine: str = ""
    pdf_path: str = ""
    message: str = ""
    compiler_log: str = ""
    page_count: int = 0
    size_bytes: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def latex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def available_latex_engine() -> str:
    for engine in LATEX_ENGINES:
        if shutil.which(engine):
            return engine
    return ""


def keyword_score(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    hits = sum(1 for keyword in keywords if keyword and keyword.lower() in lowered)
    return min(100, int((hits / max(len(keywords), 1)) * 100))


def extract_keywords(description: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", description.lower())
    stop = {
        "and",
        "the",
        "for",
        "with",
        "you",
        "our",
        "are",
        "that",
        "will",
        "this",
        "from",
        "have",
        "work",
        "team",
        "role",
        "job",
        "your",
    }
    freq: dict[str, int] = {}
    for word in words:
        if word not in stop:
            freq[word] = freq.get(word, 0) + 1
    return [word for word, _ in sorted(freq.items(), key=lambda item: (-item[1], item[0]))[:16]]


def generate_resume_tex(profile: str, job: dict[str, object], destination: Path) -> str:
    title = str(job.get("title", "Target Role"))
    company = str(job.get("company", "Target Company"))
    description = str(job.get("description", ""))
    keywords = extract_keywords(description)
    profile_lines = [
        line.strip("-* \t")
        for line in profile.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    evidence = profile_lines[:12] or ["Add resume, transcripts, and notes to docs/."]
    candidate_name = evidence[0]
    bullets = evidence[1:7] or evidence[:6]
    keyword_line = ", ".join(keywords[:10]) or "role-aligned experience"
    tex = rf"""
\documentclass[10pt,letterpaper]{{article}}
\usepackage[margin=0.65in]{{geometry}}
\usepackage{{enumitem}}
\usepackage{{hyperref}}
\setlength{{\parindent}}{{0pt}}
\setlist[itemize]{{leftmargin=*, noitemsep, topsep=2pt}}
\pagestyle{{empty}}

\begin{{document}}

{{\LARGE {latex_escape(candidate_name)}}}\\
\textbf{{Target role:}} {latex_escape(title)} at {latex_escape(company)}\\
\textbf{{Focus keywords:}} {latex_escape(keyword_line)}

\vspace{{8pt}}
\textbf{{Professional Summary}}\\
Candidate profile tailored for {latex_escape(title)} using uploaded source documents. This draft emphasizes evidence that appears relevant to {latex_escape(company)} and should be reviewed before submission.

\vspace{{8pt}}
\textbf{{Relevant Experience and Evidence}}
\begin{{itemize}}
{chr(10).join(f"  \\item {latex_escape(item[:220])}" for item in bullets)}
\end{{itemize}}

\vspace{{8pt}}
\textbf{{Role Alignment}}
\begin{{itemize}}
  \item Tailored against the supplied job description and prioritized recurring skills, tools, and responsibilities.
  \item Generated as LaTeX first so every application keeps a reproducible source file.
  \item Review for accuracy before autonomous submission; unsupported claims should be removed or backed by docs.
\end{{itemize}}

\end{{document}}
""".strip()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(tex + "\n", encoding="utf-8")
    return str(destination)


def validate_pdf(pdf_path: Path) -> tuple[int, int, str]:
    if not pdf_path.exists():
        return 0, 0, "Compiler did not produce a PDF."
    size_bytes = pdf_path.stat().st_size
    if size_bytes < 1_000:
        return 0, size_bytes, "Generated PDF is unexpectedly small."
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path), strict=False)
        if reader.is_encrypted:
            return 0, size_bytes, "Generated PDF is unexpectedly encrypted."
        page_count = len(reader.pages)
        if page_count < 1:
            return 0, size_bytes, "Generated PDF has no pages."
        extracted = []
        for page in reader.pages[: min(page_count, 3)]:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            if width <= 0 or height <= 0:
                return 0, size_bytes, "Generated PDF contains invalid page geometry."
            extracted.append(page.extract_text() or "")
        if len("".join(extracted).strip()) < 20:
            return page_count, size_bytes, "Generated PDF contains too little extractable text."
    except Exception as exc:
        return 0, size_bytes, f"Generated PDF validation failed: {exc}"
    return page_count, size_bytes, ""


def _trim_log(value: str, limit: int = 12_000) -> str:
    cleaned = value.strip()
    return cleaned if len(cleaned) <= limit else cleaned[-limit:]


def compile_pdf(tex_path: Path, timeout_seconds: int = 180) -> CompilationResult:
    tex_path = tex_path.resolve()
    pdf_path = tex_path.with_suffix(".pdf")
    pdf_path.unlink(missing_ok=True)
    engine = available_latex_engine()
    if not engine:
        message = "No TeX engine was found. Install Tectonic and compile again."
        log(message, "warning", {"tex": str(tex_path)})
        return CompilationResult(status="unavailable", message=message)
    if not tex_path.exists() or tex_path.suffix.lower() != ".tex":
        message = "LaTeX source file is missing or invalid."
        log(message, "error", {"tex": str(tex_path)})
        return CompilationResult(status="failed", engine=engine, message=message)
    started = time.monotonic()
    if engine == "tectonic":
        command = [
            engine,
            "--untrusted",
            "--keep-logs",
            "--color",
            "never",
            "--outdir",
            str(tex_path.parent),
            str(tex_path),
        ]
    else:
        command = [engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", tex_path.name]
    try:
        completed = subprocess.run(
            command,
            cwd=tex_path.parent,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_seconds),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        compiler_log = _trim_log("\n".join(part for part in (completed.stdout, completed.stderr) if part))
        if completed.returncode != 0:
            message = f"LaTeX compilation failed with exit code {completed.returncode}."
            log(message, "error", {"engine": engine, "tex": str(tex_path), "log": compiler_log[-2000:]})
            return CompilationResult(
                status="failed",
                engine=engine,
                message=message,
                compiler_log=compiler_log,
                duration_ms=duration_ms,
            )
        page_count, size_bytes, validation_error = validate_pdf(pdf_path)
        if validation_error:
            pdf_path.unlink(missing_ok=True)
            log(validation_error, "error", {"engine": engine, "tex": str(tex_path)})
            return CompilationResult(
                status="invalid",
                engine=engine,
                message=validation_error,
                compiler_log=compiler_log,
                page_count=page_count,
                size_bytes=size_bytes,
                duration_ms=duration_ms,
            )
        message = f"Compiled and validated {page_count}-page resume PDF with {engine}."
        log(message, meta={"pdf": str(pdf_path), "size_bytes": size_bytes, "duration_ms": duration_ms})
        return CompilationResult(
            status="compiled",
            engine=engine,
            pdf_path=str(pdf_path),
            message=message,
            compiler_log=compiler_log,
            page_count=page_count,
            size_bytes=size_bytes,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        pdf_path.unlink(missing_ok=True)
        compiler_log = _trim_log("\n".join(str(part or "") for part in (exc.stdout, exc.stderr)))
        message = f"LaTeX compilation timed out after {timeout_seconds} seconds."
        log(message, "error", {"engine": engine, "tex": str(tex_path)})
        return CompilationResult(
            status="timeout",
            engine=engine,
            message=message,
            compiler_log=compiler_log,
            duration_ms=duration_ms,
        )
    except subprocess.CalledProcessError as exc:
        message = "LaTeX compiler could not be executed."
        compiler_log = _trim_log(str(exc.stderr or exc))
        log(message, "error", {"engine": engine, "tex": str(tex_path), "log": compiler_log[-2000:]})
        return CompilationResult(status="failed", engine=engine, message=message, compiler_log=compiler_log)
    except OSError as exc:
        message = f"Could not run LaTeX compiler: {exc}"
        log(message, "error", {"engine": engine, "tex": str(tex_path)})
        return CompilationResult(status="failed", engine=engine, message=message)


def application_dir(application_id: int) -> Path:
    path = GENERATED_DIR / "applications" / str(application_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
