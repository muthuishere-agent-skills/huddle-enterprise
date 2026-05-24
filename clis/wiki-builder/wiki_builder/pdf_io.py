"""PDF text extraction. Uses pymupdf (fast) if available, falls back to pdfplumber."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Page:
    number: int  # 1-indexed
    text: str


def extract_pages(pdf_path: Path) -> list[Page]:
    pdf_path = Path(pdf_path)
    try:
        import fitz  # pymupdf
        return _extract_with_pymupdf(pdf_path)
    except ImportError:
        pass
    import pdfplumber
    return _extract_with_pdfplumber(pdf_path)


def _extract_with_pymupdf(pdf_path: Path) -> list[Page]:
    import fitz
    pages: list[Page] = []
    with fitz.open(str(pdf_path)) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            pages.append(Page(number=i, text=text))
    return pages


def _extract_with_pdfplumber(pdf_path: Path) -> list[Page]:
    import pdfplumber
    pages: list[Page] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, p in enumerate(pdf.pages, start=1):
            pages.append(Page(number=i, text=p.extract_text() or ""))
    return pages


def extract_markdown(path: Path) -> list[Page]:
    """Treat a markdown/text file as a single page."""
    return [Page(number=1, text=Path(path).read_text(encoding="utf-8", errors="replace"))]
