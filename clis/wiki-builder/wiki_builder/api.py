"""FastAPI surface. Endpoint set mirrors the CLI 1:1."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from pydantic import BaseModel, Field

from . import storage as st
from .ingest import ingest_file
from . import s3_sync

app = FastAPI(title="wiki-builder", version="0.1.0")


# ---------- models ----------

class CorpusCreate(BaseModel):
    name: str
    description: str | None = None


class ChunkCreate(BaseModel):
    text: str
    section: str | None = None
    summary: str | None = None
    concepts: list[str] = Field(default_factory=list)
    definitions: dict[str, str] = Field(default_factory=dict)
    claims: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    xrefs: list[str] = Field(default_factory=list)


class ChunkPatch(BaseModel):
    text: str | None = None
    section: str | None = None
    summary: str | None = None
    concepts: list[str] | None = None
    definitions: dict[str, str] | None = None
    claims: list[str] | None = None
    examples: list[str] | None = None
    xrefs: list[str] | None = None
    page_start: int | None = None
    page_end: int | None = None


class SearchRequest(BaseModel):
    q: str
    k: int = 5


class IngestRequest(BaseModel):
    path: str
    model: str = "sonnet"
    workers: int = 4


class S3Request(BaseModel):
    s3_uri: str
    profile: str | None = None


# ---------- corpora ----------

@app.get("/corpora")
def list_corpora():
    return st.list_corpora()


@app.post("/corpora", status_code=201)
def create_corpus(body: CorpusCreate):
    with st.Store(body.name) as s:
        if body.description:
            s.set_description(body.description)
    return {"corpus": body.name, "created": True}


@app.delete("/corpora/{corpus}")
def delete_corpus(corpus: str, confirm: bool = False):
    if not confirm:
        raise HTTPException(400, "pass ?confirm=true to drop the corpus")
    ok = st.drop_corpus(corpus)
    return {"deleted": corpus, "existed": ok}


@app.get("/corpora/{corpus}/stats")
def corpus_stats(corpus: str):
    with st.Store(corpus, read_only=True) as s:
        return s.stats()


# ---------- docs ----------

@app.get("/corpora/{corpus}/docs")
def list_docs(corpus: str):
    with st.Store(corpus, read_only=True) as s:
        return s.list_docs()


@app.delete("/corpora/{corpus}/docs/{doc_id}")
def delete_doc(corpus: str, doc_id: str):
    with st.Store(corpus) as s:
        n = s.delete_doc(doc_id)
        if n:
            s.export_parquet()
    return {"deleted": doc_id, "rows": n}


@app.post("/corpora/{corpus}/ingest")
def trigger_ingest(corpus: str, body: IngestRequest, tasks: BackgroundTasks):
    p = Path(body.path).expanduser()
    if not p.exists():
        raise HTTPException(404, f"path not found: {body.path}")
    # Run in background so the request returns quickly with a job ack.
    tasks.add_task(_run_ingest_sync, p, corpus, body.model, body.workers)
    return {"status": "queued", "path": str(p), "corpus": corpus}


@app.post("/corpora/{corpus}/upload")
async def upload_and_ingest(
    corpus: str,
    file: UploadFile = File(...),
    model: str = Form("sonnet"),
    workers: int = Form(4),
    tasks: BackgroundTasks = None,
):
    """Multipart upload alternative to /ingest. Saves to /tmp, queues ingest."""
    import tempfile
    suffix = Path(file.filename or "doc").suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    tasks.add_task(_run_ingest_sync, tmp_path, corpus, model, workers)
    return {"status": "queued", "stored_at": str(tmp_path), "corpus": corpus}


def _run_ingest_sync(path: Path, corpus: str, model: str, workers: int) -> None:
    try:
        ingest_file(path, corpus=corpus, model=model, max_workers=workers, on_progress=None)
    except Exception as e:
        # Background task — log via print since we don't have a logger wired.
        import sys
        print(f"[ingest error] {path}: {e}", file=sys.stderr)


# ---------- chunks ----------

@app.get("/corpora/{corpus}/chunks")
def list_chunks(corpus: str, doc: str | None = None, limit: int = 100, offset: int = 0):
    with st.Store(corpus, read_only=True) as s:
        return s.list_chunks(doc_id=doc, limit=limit, offset=offset)


@app.get("/corpora/{corpus}/chunks/{chunk_id}")
def get_chunk(corpus: str, chunk_id: str):
    with st.Store(corpus, read_only=True) as s:
        c = s.get_chunk(chunk_id)
    if not c:
        raise HTTPException(404, "chunk not found")
    return c


@app.post("/corpora/{corpus}/chunks", status_code=201)
def add_chunk(corpus: str, body: ChunkCreate):
    with st.Store(corpus) as s:
        doc_id = f"{corpus}#manual"
        s.upsert_doc(
            doc_id=doc_id, source_path=None, title="manual",
            doc_type="manual", num_pages=None, model=None,
        )
        existing = s.list_chunks(doc_id=doc_id, limit=1_000_000)
        ordinal = max([c["ordinal"] for c in existing], default=-1) + 1
        slug = st.slugify(body.section or body.text[:40])
        chunk = st.Chunk(
            chunk_id=f"{doc_id}#{ordinal:04d}-{slug}",
            doc_id=doc_id, corpus=corpus, ordinal=ordinal,
            text=body.text, section=body.section, summary=body.summary,
            concepts=body.concepts, definitions=body.definitions,
            claims=body.claims, examples=body.examples, xrefs=body.xrefs,
        )
        s.upsert_chunk(chunk)
        s.export_parquet()
    return {"chunk_id": chunk.chunk_id, "ordinal": ordinal}


@app.put("/corpora/{corpus}/chunks/{chunk_id}")
def patch_chunk(corpus: str, chunk_id: str, body: ChunkPatch):
    patch = {k: v for k, v in body.dict().items() if v is not None}
    if not patch:
        raise HTTPException(400, "empty patch")
    with st.Store(corpus) as s:
        ok = s.patch_chunk(chunk_id, patch)
        if ok:
            s.export_parquet()
    if not ok:
        raise HTTPException(404, "chunk not found")
    return {"updated": chunk_id}


@app.delete("/corpora/{corpus}/chunks/{chunk_id}")
def delete_chunk(corpus: str, chunk_id: str):
    with st.Store(corpus) as s:
        ok = s.delete_chunk(chunk_id)
        if ok:
            s.export_parquet()
    if not ok:
        raise HTTPException(404, "chunk not found")
    return {"deleted": chunk_id}


# ---------- search ----------

@app.post("/corpora/{corpus}/search")
def search(corpus: str, body: SearchRequest):
    with st.Store(corpus, read_only=True) as s:
        return s.search(body.q, k=body.k)


# ---------- export / pull ----------

@app.post("/corpora/{corpus}/export")
def export(corpus: str, body: S3Request | None = None):
    with st.Store(corpus, read_only=True) as s:
        targets = s.export_parquet()
    out: dict[str, Any] = {k: str(v) for k, v in targets.items()}
    if body and body.s3_uri:
        out["uploaded"] = s3_sync.push(corpus, body.s3_uri, profile=body.profile)
    return out


@app.post("/corpora/{corpus}/pull")
def pull(corpus: str, body: S3Request):
    files = s3_sync.pull(corpus, body.s3_uri, profile=body.profile)
    return {"downloaded": files}


# ---------- qa surface (read-only here; the autonomous huddle writes) ----------

@app.get("/corpora/{corpus}/qa")
def recent_qa(corpus: str, limit: int = 10):
    with st.Store(corpus, read_only=True) as s:
        return s.recent_qa(limit=limit)
