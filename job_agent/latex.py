from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .config import GENERATED_DIR, LATEX_ENGINES
from .db import log


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
    profile_lines = [line.strip("-* \t") for line in profile.splitlines() if line.strip()]
    evidence = profile_lines[:12] or ["Add resume, transcripts, and notes to docs/."]
    bullets = evidence[:6]
    keyword_line = ", ".join(keywords[:10]) or "role-aligned experience"
    tex = rf"""
\documentclass[10pt,letterpaper]{{article}}
\usepackage[margin=0.65in]{{geometry}}
\usepackage{{enumitem}}
\usepackage{{hyperref}}
\setlength{{\parindent}}{{0pt}}
\setlist[itemize]{{leftmargin=*, noitemsep, topsep=2pt}}

\begin{{document}}

{{\LARGE Tailored Resume Draft}}\\
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


def compile_pdf(tex_path: Path) -> str:
    engine = available_latex_engine()
    if not engine:
        log("LaTeX source generated, but no TeX engine was found for PDF compilation.", "warning")
        return ""
    try:
        if engine == "tectonic":
            subprocess.run([engine, str(tex_path)], cwd=tex_path.parent, check=True, capture_output=True, text=True)
        else:
            subprocess.run(
                [engine, "-interaction=nonstopmode", tex_path.name],
                cwd=tex_path.parent,
                check=True,
                capture_output=True,
                text=True,
            )
        pdf = tex_path.with_suffix(".pdf")
        if pdf.exists():
            log(f"Compiled resume PDF with {engine}.", meta={"pdf": str(pdf)})
            return str(pdf)
    except subprocess.CalledProcessError as exc:
        log("LaTeX compilation failed; review the .tex file.", "error", {"stderr": exc.stderr[-2000:]})
    return ""


def application_dir(application_id: int) -> Path:
    path = GENERATED_DIR / "applications" / str(application_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
