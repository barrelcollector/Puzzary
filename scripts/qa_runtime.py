"""Cross-platform paths for isolated verification scripts."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def qa_directory(name: str) -> Path:
    """Return a check-specific directory inside the disposable QA root."""
    base = Path(os.environ.get("PUZZARY_QA_ROOT", tempfile.gettempdir()))
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return path
