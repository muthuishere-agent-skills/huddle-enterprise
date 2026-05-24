"""Page-grouped chunker. Deterministic — same input gives same chunk_ids."""
from __future__ import annotations
import re
from dataclasses import dataclass
from .pdf_io import Page

# Rough budget. Claude can handle far more, but extraction quality drops past ~2k tokens.
# 1 token ≈ 4 chars for English prose.
MAX_CHARS = 6000
MIN_CHARS = 500
OVERLAP_CHARS = 200


@dataclass
class RawChunk:
    ordinal: int
    page_start: int
    page_end: int
    text: str


def chunk_pages(pages: list[Page]) -> list[RawChunk]:
    """Group consecutive pages into ~MAX_CHARS chunks. Adds light overlap between chunks."""
    chunks: list[RawChunk] = []
    buf_text: list[str] = []
    buf_pages: list[int] = []
    buf_len = 0
    ord_counter = 0

    def flush():
        nonlocal ord_counter, buf_text, buf_pages, buf_len
        if not buf_text:
            return
        text = _clean("\n\n".join(buf_text))
        chunks.append(RawChunk(
            ordinal=ord_counter,
            page_start=buf_pages[0],
            page_end=buf_pages[-1],
            text=text,
        ))
        ord_counter += 1
        # carry overlap from tail of this chunk into next
        if OVERLAP_CHARS and len(text) > OVERLAP_CHARS:
            tail = text[-OVERLAP_CHARS:]
            buf_text = [tail]
            buf_pages = [buf_pages[-1]]
            buf_len = len(tail)
        else:
            buf_text = []
            buf_pages = []
            buf_len = 0

    for page in pages:
        ptext = _clean(page.text)
        if not ptext.strip():
            continue
        # If page alone would already overflow, split it into pieces.
        if len(ptext) > MAX_CHARS:
            for piece in _split_long(ptext, MAX_CHARS):
                buf_text.append(piece)
                buf_pages.append(page.number)
                buf_len += len(piece)
                if buf_len >= MAX_CHARS:
                    flush()
            continue
        if buf_len + len(ptext) > MAX_CHARS and buf_len >= MIN_CHARS:
            flush()
        buf_text.append(ptext)
        buf_pages.append(page.number)
        buf_len += len(ptext)

    if buf_text:
        flush()
    return chunks


def _clean(text: str) -> str:
    # Collapse repeated whitespace but preserve paragraph breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_long(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    para = text.split("\n\n")
    buf: list[str] = []
    buf_len = 0
    for p in para:
        if buf_len + len(p) > max_chars and buf:
            pieces.append("\n\n".join(buf))
            buf = [p]
            buf_len = len(p)
        else:
            buf.append(p)
            buf_len += len(p) + 2
    if buf:
        pieces.append("\n\n".join(buf))
    # If a single paragraph is still too long, hard-split.
    out: list[str] = []
    for p in pieces:
        if len(p) <= max_chars:
            out.append(p)
        else:
            for i in range(0, len(p), max_chars):
                out.append(p[i:i + max_chars])
    return out
