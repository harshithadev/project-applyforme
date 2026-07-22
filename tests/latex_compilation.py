from __future__ import annotations

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def test_root() -> Iterator[Path]:
    qa_root = os.environ.get("APPLYFORME_QA_ROOT", "").strip()
    if qa_root:
        root = Path(qa_root).resolve()
        allowed_root = (REPO_ROOT / "tmp").resolve()
        if root == allowed_root or allowed_root not in root.parents:
            raise ValueError("APPLYFORME_QA_ROOT must be a child of this repository's tmp/ directory.")
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        yield root
        return
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with test_root() as root:
        os.environ["APPLYFORME_ROOT"] = str(root)

        from pypdf import PdfReader

        from job_agent import applications, jobs, latex, profile
        from job_agent.config import DOCS_DIR
        from job_agent.db import init_db, row

        assert latex.available_latex_engine(), "Install Tectonic or another supported TeX engine."
        init_db()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        (DOCS_DIR / "resume.md").write_text(
            "Alex Candidate\n"
            "Platform engineer using Python, TypeScript, SQL, and cloud automation.\n"
            "Built reliable APIs and reduced deployment time by 40 percent.",
            encoding="utf-8",
        )
        assert profile.ingest_docs()["ingested"] == 1

        job_id = jobs.add_manual_job(
            {
                "title": "R&D_Platform Engineer",
                "company": "Example & Company",
                "url": "https://example.test/jobs/platform-engineer",
                "description": "Build Python and TypeScript services, reliable APIs, SQL systems, and cloud automation.",
                "location": "Remote",
            }
        )
        app = applications.draft_application(job_id)
        tex_path = Path(str(app["resume_tex_path"]))
        pdf_path = Path(str(app["resume_pdf_path"]))

        assert app["resume_compile_status"] == "compiled", app["resume_compile_message"]
        assert app["resume_compile_engine"] == latex.available_latex_engine()
        assert int(app["resume_pdf_pages"]) >= 1
        assert int(app["resume_pdf_bytes"]) >= 1_000
        assert tex_path.is_file() and pdf_path.is_file()
        assert r"R\&D\_Platform Engineer" in tex_path.read_text(encoding="utf-8")

        reader = PdfReader(str(pdf_path), strict=False)
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "Example & Company" in pdf_text
        assert "Alex Candidate" in pdf_text
        pages, size_bytes, validation_error = latex.validate_pdf(pdf_path)
        assert pages >= 1 and size_bytes >= 1_000 and not validation_error

        recompiled = applications.recompile_application(int(app["id"]))
        assert recompiled["resume_compile_status"] == "compiled"
        assert Path(str(recompiled["resume_pdf_path"])).is_file()

        tex_path.write_text(r"\documentclass{article}\begin{document}\undefinedcontrol\end{document}", encoding="utf-8")
        failed = applications.recompile_application(int(app["id"]))
        assert failed["resume_compile_status"] == "failed"
        assert not failed["resume_pdf_path"]
        assert failed["resume_compile_message"]
        assert failed["resume_compile_log"]
        assert not pdf_path.exists(), "A stale PDF must not survive a failed compilation."

        job = row("SELECT * FROM jobs WHERE id = ?", (job_id,))
        assert job is not None
        latex.generate_resume_tex(profile.profile_text(), job, tex_path)
        restored = applications.recompile_application(int(app["id"]))
        assert restored["resume_compile_status"] == "compiled"

        no_engine_tex = root / "no-engine.tex"
        no_engine_tex.write_text(r"\documentclass{article}\begin{document}Test\end{document}", encoding="utf-8")
        no_engine_pdf = no_engine_tex.with_suffix(".pdf")
        no_engine_pdf.write_bytes(b"stale output")
        original_engine = latex.available_latex_engine
        try:
            latex.available_latex_engine = lambda: ""
            unavailable = latex.compile_pdf(no_engine_tex)
        finally:
            latex.available_latex_engine = original_engine
        assert unavailable.status == "unavailable"
        assert not no_engine_pdf.exists(), "A stale PDF must be removed before every compile attempt."

        print(f"latex compilation ok: {restored['resume_pdf_path']}")


if __name__ == "__main__":
    main()
