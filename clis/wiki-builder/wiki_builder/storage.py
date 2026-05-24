"""DuckDB-backed storage for the LLM wiki. CRUD for corpora, docs, chunks, qa."""
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from . import paths

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS corpora (
    name        TEXT PRIMARY KEY,
    description TEXT,
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    doc_id       TEXT PRIMARY KEY,
    corpus       TEXT NOT NULL,
    source_path  TEXT,
    title        TEXT,
    doc_type     TEXT,
    num_pages    INTEGER,
    ingested_at  TIMESTAMP NOT NULL,
    model        TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    corpus       TEXT NOT NULL,
    ordinal      INTEGER NOT NULL,
    page_start   INTEGER,
    page_end     INTEGER,
    section      TEXT,
    text         TEXT NOT NULL,
    summary      TEXT,
    concepts     TEXT[],
    definitions  JSON,
    claims       TEXT[],
    examples     TEXT[],
    xrefs        TEXT[],
    created_at   TIMESTAMP NOT NULL,
    updated_at   TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_corpus  ON chunks(corpus);
CREATE INDEX IF NOT EXISTS idx_chunks_doc     ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_docs_corpus    ON docs(corpus);

CREATE TABLE IF NOT EXISTS qa (
    qa_id       TEXT PRIMARY KEY,
    corpus      TEXT NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT,
    dissent     JSON,
    personas    TEXT[],
    chunk_refs  TEXT[],
    asked_at    TIMESTAMP NOT NULL
);

-- Forward-compatible payload column for full Answer JSON (perspectives, open_questions,
-- next_branches, amara_research, etc.). Older rows have NULL.
ALTER TABLE qa ADD COLUMN IF NOT EXISTS payload JSON;
ALTER TABLE qa ADD COLUMN IF NOT EXISTS asker TEXT;

CREATE INDEX IF NOT EXISTS idx_qa_corpus_q ON qa(corpus, question);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.strip().lower()).strip("-")
    return s[:maxlen] or "x"


def content_hash(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8") if isinstance(p, str) else p)
    return h.hexdigest()[:16]


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    corpus: str
    ordinal: int
    text: str
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    summary: str | None = None
    concepts: list[str] = field(default_factory=list)
    definitions: dict[str, str] = field(default_factory=dict)
    claims: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    xrefs: list[str] = field(default_factory=list)


class Store:
    """Per-corpus DuckDB store. Use `with Store(corpus) as s: ...`."""

    def __init__(self, corpus: str, *, read_only: bool = False):
        self.corpus = corpus
        paths.ensure_corpus_dirs(corpus)
        self.path = paths.db_path(corpus)
        # Always run migrations even when the caller wants read-only — an existing
        # DuckDB file may have an older schema, and the read-only connection can't
        # ALTER. Migrations are idempotent (IF NOT EXISTS everywhere).
        if self.path.exists():
            migrate = duckdb.connect(str(self.path))
            try:
                migrate.execute(SCHEMA_SQL)
            finally:
                migrate.close()
        self.conn = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self.conn.execute(SCHEMA_SQL)
            self._ensure_corpus_row()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.close()

    # ---------- corpora ----------

    def _ensure_corpus_row(self) -> None:
        row = self.conn.execute(
            "SELECT name FROM corpora WHERE name = ?", [self.corpus]
        ).fetchone()
        if row is None:
            ts = now()
            self.conn.execute(
                "INSERT INTO corpora(name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                [self.corpus, None, ts, ts],
            )

    def set_description(self, description: str) -> None:
        self.conn.execute(
            "UPDATE corpora SET description = ?, updated_at = ? WHERE name = ?",
            [description, now(), self.corpus],
        )

    def stats(self) -> dict[str, Any]:
        n_docs = self.conn.execute(
            "SELECT count(*) FROM docs WHERE corpus = ?", [self.corpus]
        ).fetchone()[0]
        n_chunks = self.conn.execute(
            "SELECT count(*) FROM chunks WHERE corpus = ?", [self.corpus]
        ).fetchone()[0]
        n_concepts = self.conn.execute(
            "SELECT count(DISTINCT c) FROM (SELECT unnest(concepts) AS c FROM chunks WHERE corpus = ?) t",
            [self.corpus],
        ).fetchone()[0]
        return {
            "corpus": self.corpus,
            "docs": n_docs,
            "chunks": n_chunks,
            "distinct_concepts": n_concepts,
            "db_path": str(self.path),
        }

    # ---------- docs ----------

    def upsert_doc(
        self,
        *,
        doc_id: str,
        source_path: str | None,
        title: str | None,
        doc_type: str,
        num_pages: int | None,
        model: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO docs(doc_id, corpus, source_path, title, doc_type, num_pages, ingested_at, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                source_path = EXCLUDED.source_path,
                title       = EXCLUDED.title,
                doc_type    = EXCLUDED.doc_type,
                num_pages   = EXCLUDED.num_pages,
                ingested_at = EXCLUDED.ingested_at,
                model       = EXCLUDED.model
            """,
            [doc_id, self.corpus, source_path, title, doc_type, num_pages, now(), model],
        )

    def delete_doc(self, doc_id: str) -> int:
        self.conn.execute(
            "DELETE FROM chunks WHERE corpus = ? AND doc_id = ?",
            [self.corpus, doc_id],
        )
        res = self.conn.execute(
            "DELETE FROM docs WHERE corpus = ? AND doc_id = ? RETURNING doc_id",
            [self.corpus, doc_id],
        ).fetchall()
        return len(res)

    def list_docs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT doc_id, source_path, title, doc_type, num_pages, ingested_at, model "
            "FROM docs WHERE corpus = ? ORDER BY ingested_at DESC",
            [self.corpus],
        ).fetchall()
        cols = ["doc_id", "source_path", "title", "doc_type", "num_pages", "ingested_at", "model"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------- chunks ----------

    def upsert_chunk(self, chunk: Chunk) -> None:
        ts = now()
        self.conn.execute(
            """
            INSERT INTO chunks(
                chunk_id, doc_id, corpus, ordinal, page_start, page_end, section,
                text, summary, concepts, definitions, claims, examples, xrefs,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chunk_id) DO UPDATE SET
                ordinal     = EXCLUDED.ordinal,
                page_start  = EXCLUDED.page_start,
                page_end    = EXCLUDED.page_end,
                section     = EXCLUDED.section,
                text        = EXCLUDED.text,
                summary     = EXCLUDED.summary,
                concepts    = EXCLUDED.concepts,
                definitions = EXCLUDED.definitions,
                claims      = EXCLUDED.claims,
                examples    = EXCLUDED.examples,
                xrefs       = EXCLUDED.xrefs,
                updated_at  = EXCLUDED.updated_at
            """,
            [
                chunk.chunk_id, chunk.doc_id, chunk.corpus, chunk.ordinal,
                chunk.page_start, chunk.page_end, chunk.section,
                chunk.text, chunk.summary,
                chunk.concepts, json.dumps(chunk.definitions or {}),
                chunk.claims, chunk.examples, chunk.xrefs,
                ts, ts,
            ],
        )

    def patch_chunk(self, chunk_id: str, patch: dict[str, Any]) -> bool:
        allowed = {
            "section", "text", "summary", "concepts", "definitions",
            "claims", "examples", "xrefs", "page_start", "page_end",
        }
        fields = {k: v for k, v in patch.items() if k in allowed}
        if not fields:
            return False
        sets = []
        params: list[Any] = []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            params.append(json.dumps(v) if k == "definitions" else v)
        sets.append("updated_at = ?")
        params.append(now())
        params.extend([self.corpus, chunk_id])
        res = self.conn.execute(
            f"UPDATE chunks SET {', '.join(sets)} WHERE corpus = ? AND chunk_id = ? RETURNING chunk_id",
            params,
        ).fetchall()
        return bool(res)

    def delete_chunk(self, chunk_id: str) -> bool:
        res = self.conn.execute(
            "DELETE FROM chunks WHERE corpus = ? AND chunk_id = ? RETURNING chunk_id",
            [self.corpus, chunk_id],
        ).fetchall()
        return bool(res)

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        rows = self.conn.execute(
            "SELECT chunk_id, doc_id, corpus, ordinal, page_start, page_end, section, "
            "text, summary, concepts, definitions, claims, examples, xrefs, created_at, updated_at "
            "FROM chunks WHERE corpus = ? AND chunk_id = ?",
            [self.corpus, chunk_id],
        ).fetchall()
        if not rows:
            return None
        return self._chunk_row_to_dict(rows[0])

    def list_chunks(
        self, *, doc_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        sql = ("SELECT chunk_id, doc_id, corpus, ordinal, page_start, page_end, section, "
               "text, summary, concepts, definitions, claims, examples, xrefs, created_at, updated_at "
               "FROM chunks WHERE corpus = ?")
        params: list[Any] = [self.corpus]
        if doc_id:
            sql += " AND doc_id = ?"
            params.append(doc_id)
        sql += " ORDER BY doc_id, ordinal LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.conn.execute(sql, params).fetchall()
        return [self._chunk_row_to_dict(r) for r in rows]

    def search(self, query: str, *, k: int = 5) -> list[dict[str, Any]]:
        """Keyword + concept ranking. Cheap baseline; vector search later."""
        tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_-]+", query) if len(t) > 2]
        if not tokens:
            return []
        like_clauses = " OR ".join(["lower(text) LIKE ?"] * len(tokens))
        concept_clauses = " OR ".join(["list_contains(concepts, ?)"] * len(tokens))
        params: list[Any] = (
            [f"%{t}%" for t in tokens]
            + tokens
            + [self.corpus, k]
        )
        sql = f"""
            SELECT chunk_id, doc_id, corpus, ordinal, page_start, page_end, section,
                   text, summary, concepts, definitions, claims, examples, xrefs,
                   created_at, updated_at,
                   (
                       (CASE WHEN {like_clauses} THEN 1 ELSE 0 END)
                       + (CASE WHEN {concept_clauses} THEN 2 ELSE 0 END)
                   ) AS score
            FROM chunks
            WHERE corpus = ?
              AND (({like_clauses}) OR ({concept_clauses}))
            ORDER BY score DESC, ordinal ASC
            LIMIT ?
        """
        # Order of placeholders in SQL: like(score), concept(score), corpus, like(where), concept(where), limit
        params = (
            [f"%{t}%" for t in tokens]
            + tokens
            + [self.corpus]
            + [f"%{t}%" for t in tokens]
            + tokens
            + [k]
        )
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = self._chunk_row_to_dict(r[:-1])
            d["score"] = r[-1]
            out.append(d)
        return out

    def _chunk_row_to_dict(self, r: tuple) -> dict[str, Any]:
        cols = [
            "chunk_id", "doc_id", "corpus", "ordinal", "page_start", "page_end", "section",
            "text", "summary", "concepts", "definitions", "claims", "examples", "xrefs",
            "created_at", "updated_at",
        ]
        d = dict(zip(cols, r))
        if isinstance(d.get("definitions"), str):
            try:
                d["definitions"] = json.loads(d["definitions"])
            except Exception:
                pass
        return d

    # ---------- qa ----------

    def record_qa(
        self,
        *,
        question: str,
        answer: str,
        dissent: list[dict] | None = None,
        personas: list[str] | None = None,
        chunk_refs: list[str] | None = None,
        payload: dict | None = None,
        asker: str | None = None,
    ) -> str:
        ts = now()
        qa_id = f"qa-{ts.strftime('%Y%m%dT%H%M%SZ')}-{slugify(question, 24)}"
        self.conn.execute(
            "INSERT INTO qa(qa_id, corpus, question, answer, dissent, personas, chunk_refs, "
            "payload, asker, asked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [qa_id, self.corpus, question, answer, json.dumps(dissent or []),
             personas or [], chunk_refs or [],
             json.dumps(payload) if payload else None,
             asker, ts],
        )
        return qa_id

    def find_cached_qa(self, question: str, *, max_age_hours: int = 168) -> dict[str, Any] | None:
        """Exact-match cache lookup. Normalized: lowercased, whitespace-collapsed.
        Returns the most recent cached entry within max_age_hours, with full payload if stored.
        """
        norm = " ".join(question.lower().strip().split())
        if not norm:
            return None
        rows = self.conn.execute(
            "SELECT qa_id, question, answer, dissent, personas, chunk_refs, payload, asked_at "
            "FROM qa WHERE corpus = ? "
            "AND lower(regexp_replace(question, '\\s+', ' ', 'g')) = ? "
            "AND asked_at > (now() - INTERVAL (?) HOUR) "
            "ORDER BY asked_at DESC LIMIT 1",
            [self.corpus, norm, max_age_hours],
        ).fetchall()
        if not rows:
            return None
        cols = ["qa_id", "question", "answer", "dissent", "personas", "chunk_refs",
                "payload", "asked_at"]
        d = dict(zip(cols, rows[0]))
        for f in ("dissent", "payload"):
            if isinstance(d.get(f), str):
                try:
                    d[f] = json.loads(d[f])
                except Exception:
                    pass
        return d

    def recent_qa(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT qa_id, question, answer, personas, chunk_refs, asked_at "
            "FROM qa WHERE corpus = ? ORDER BY asked_at DESC LIMIT ?",
            [self.corpus, limit],
        ).fetchall()
        cols = ["qa_id", "question", "answer", "personas", "chunk_refs", "asked_at"]
        return [dict(zip(cols, r)) for r in rows]

    # ---------- export / import ----------

    def export_parquet(self, out_dir: Path | None = None) -> dict[str, Path]:
        out = Path(out_dir) if out_dir else paths.exports_dir(self.corpus)
        out.mkdir(parents=True, exist_ok=True)
        targets = {
            "corpora": out / "corpora.parquet",
            "docs":    out / "docs.parquet",
            "chunks":  out / "chunks.parquet",
            "qa":      out / "qa.parquet",
        }
        for table, target in targets.items():
            where = "" if table == "corpora" else f" WHERE corpus = '{self.corpus}'"
            if table == "corpora":
                where = f" WHERE name = '{self.corpus}'"
            self.conn.execute(
                f"COPY (SELECT * FROM {table}{where}) TO '{target}' (FORMAT PARQUET)"
            )
        return targets


def list_corpora() -> list[dict[str, Any]]:
    """Walks the root looking for wiki.duckdb files. Returns each corpus + stats."""
    out: list[dict[str, Any]] = []
    root = paths.root()
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        db = child / "wiki.duckdb"
        if not db.exists():
            continue
        try:
            with Store(child.name, read_only=True) as s:
                out.append(s.stats())
        except Exception as e:
            out.append({"corpus": child.name, "error": str(e)})
    return out


def drop_corpus(corpus: str) -> bool:
    """Delete the whole corpus directory."""
    import shutil
    cdir = paths.corpus_dir(corpus)
    if not cdir.exists():
        return False
    shutil.rmtree(cdir)
    return True
