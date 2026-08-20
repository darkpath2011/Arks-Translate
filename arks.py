"""Arks startup. Run: python arks.py"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

BASE_DIR = Path(__file__).parent
PAPERS_DIR = BASE_DIR / "papers"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def auto_import_demo() -> None:
    """Import the bundled test paper if it's there and not yet in DB."""
    import db
    import pdf_extract

    candidate = BASE_DIR / "csikszentmihalyi_optimalexperience_1989.pdf"
    if not candidate.exists():
        return
    # Check if already imported (by source path)
    rows = [r for r in db.list_papers() if r["source_path"] == str(candidate)]
    if rows:
        return
    paper_id = pdf_extract.extract_pdf(candidate)
    print(f"[arks] imported demo paper: id={paper_id}")


def main() -> None:
    setup_logging()
    # Ensure DB schema exists
    import db as _db
    _db.get_conn()

    auto_import_demo()

    port = int(os.environ.get("ARKS_PORT", "8765"))
    host = os.environ.get("ARKS_HOST", "127.0.0.1")
    print(f"[arks] starting on http://{host}:{port}")
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()