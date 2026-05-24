"""Orchestrates: source file → pages → chunks → claude extraction → DuckDB rows."""
from __future__ import annotations
import concurrent.futures as cf
import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import pdf_io
from .chunker import chunk_pages, RawChunk
from .extract import Extraction, extract_chunk, ExtractError
from .storage import Chunk, Store, content_hash


@dataclass
class IngestReport:
    doc_id: str
    title: str
    num_pages: int
    num_chunks: int
    extract_failures: int
    elapsed_s: float


SUPPORTED_PDF = {".pdf"}
SUPPORTED_TEXT = {".md", ".markdown", ".txt", ".rst"}


def ingest_file(
    path: Path,
    *,
    corpus: str,
    model: str = "sonnet",
    max_workers: int = 4,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> IngestReport:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    if suffix in SUPPORTED_PDF:
        pages = pdf_io.extract_pages(path)
        doc_type = "pdf"
    elif suffix in SUPPORTED_TEXT:
        pages = pdf_io.extract_markdown(path)
        doc_type = "text"
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    body = "\n".join(p.text for p in pages)
    doc_id = content_hash(str(path), body)
    chunks = chunk_pages(pages)
    total = len(chunks)
    if total == 0:
        raise ValueError(f"No extractable text in {path}")

    with Store(corpus) as store:
        store.upsert_doc(
            doc_id=doc_id,
            source_path=str(path),
            title=path.stem,
            doc_type=doc_type,
            num_pages=len(pages),
            model=model,
        )

        failures = 0
        t0 = time.time()

        def run_one(rc: RawChunk) -> tuple[RawChunk, Extraction | None, str | None]:
            try:
                ext = extract_chunk(rc.text, model=model)
                return rc, ext, None
            except ExtractError as e:
                return rc, None, str(e)

        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(run_one, rc): rc for rc in chunks}
            done = 0
            for fut in cf.as_completed(futures):
                rc, extraction, err = fut.result()
                done += 1
                if err:
                    failures += 1
                    extraction = Extraction.empty()
                    extraction.summary = f"[extraction failed: {err}]"
                chunk = Chunk(
                    chunk_id=f"{doc_id}#{rc.ordinal:04d}",
                    doc_id=doc_id,
                    corpus=corpus,
                    ordinal=rc.ordinal,
                    text=rc.text,
                    page_start=rc.page_start,
                    page_end=rc.page_end,
                    section=extraction.section or None,
                    summary=extraction.summary or None,
                    concepts=extraction.concepts,
                    definitions=extraction.definitions,
                    claims=extraction.claims,
                    examples=extraction.examples,
                    xrefs=extraction.xrefs,
                )
                store.upsert_chunk(chunk)
                if on_progress:
                    on_progress(done, total, rc.text[:60].replace("\n", " "))

        elapsed = time.time() - t0
        store.export_parquet()

    return IngestReport(
        doc_id=doc_id,
        title=path.stem,
        num_pages=len(pages),
        num_chunks=total,
        extract_failures=failures,
        elapsed_s=elapsed,
    )


def default_progress(done: int, total: int, preview: str) -> None:
    bar_len = 24
    filled = int(bar_len * done / total)
    bar = "#" * filled + "-" * (bar_len - filled)
    sys.stdout.write(f"\r[{bar}] {done}/{total}  {preview}\033[K")
    sys.stdout.flush()
    if done == total:
        sys.stdout.write("\n")
