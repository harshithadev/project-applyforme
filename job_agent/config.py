from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(os.environ.get("APPLYFORME_ROOT", Path(__file__).resolve().parent.parent)).resolve()
DOCS_DIR = ROOT / "docs"
GENERATED_DIR = ROOT / "generated"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "applyforme.sqlite3"

SUPPORTED_DOC_EXTENSIONS = {".txt", ".md", ".tex", ".csv", ".pdf", ".docx"}
LATEX_ENGINES = ("tectonic", "pdflatex", "xelatex", "lualatex")
