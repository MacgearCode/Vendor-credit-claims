"""PDF and zip handling for the vendor bill importer.

The only I/O in the feature: bytes in, extracted text out. Everything that
decides a number lives in `bill_calc.py`.
"""
from __future__ import annotations

import io
import logging
import zipfile

import pdfplumber

# Amazon's PDFs carry font descriptors pdfminer dislikes. It extracts the text
# fine; the complaints are just noise.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

MAX_FILES = 100  # a day's invoices is 1; a month is ~31. 100 is a sanity bound.


def extract_text(data: bytes) -> str:
    """All pages of one PDF as text."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def collect_pdfs(uploads: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Flatten uploaded files into (name, pdf_bytes) pairs.

    Accepts PDFs directly and expands any .zip. Anything else is ignored rather
    than raising -- a stray .DS_Store or Thumbs.db in a zip is not an error.
    Sorted by name so the CSV row order is stable between runs.
    """
    out: list[tuple[str, bytes]] = []
    for name, data in uploads:
        lower = name.lower()
        if lower.endswith(".pdf"):
            out.append((name, data))
        elif lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                        continue
                    # Zip entries keep their folder path; the leaf name is what
                    # the user recognises in the results table.
                    leaf = info.filename.rsplit("/", 1)[-1]
                    out.append((leaf, zf.read(info)))
    return sorted(out, key=lambda pair: pair[0])
