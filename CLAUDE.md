# huddle-enterprise

Productized Huddle: an autonomous, multi-persona answer engine grounded in a queryable LLM wiki built from your documents.

## What it does

```
documents ──► wiki-builder ──► DuckDB+Parquet+S3 ──┐
                                                   ▼
question ──► huddle (autonomous) ──► answer ◄──┐  query wiki + past Q&A
                                              │
                              Society-of-Mind framing for synthesis
```

Two tools work together:

1. **wiki-builder** — full-lifecycle CLI + HTTP API. Ingests documents, chunks them, extracts structured knowledge via headless Claude, stores in DuckDB, exports Parquet, mirrors to S3. Exposes manual CRUD endpoints so curated edits don't require re-ingest.
2. **huddle** (autonomous fork) — takes a question + corpus, queries the wiki, runs 3 personas internally, synthesizes one answer with dissent. No stop-and-wait. Society-of-Mind framing: each persona is a Minsky "agent".

## Layout

```
huddle-enterprise/
├── clis/
│   ├── wiki-builder/         CLI + FastAPI HTTP server for wiki CRUD
│   └── (responser later)
├── skills/
│   ├── wiki-builder/         Agent skill wrapping the CLI
│   └── huddle/               Autonomous huddle skill
├── apps/                     UI (Phase 2)
└── infra/
    └── vault/                Credentials (do not read; protected)
```

## State storage

```
~/.config/muthuishere-agent-skills/huddle-enterprise/
└── {corpus}/
    ├── wiki.duckdb           Primary store
    ├── exports/              Parquet snapshots
    │   ├── corpora.parquet
    │   ├── docs.parquet
    │   └── chunks.parquet
    └── qa/                   Past Q&A (autonomous huddle output)
        └── {ts}_{slug}.json
```

S3 mirror: `s3://{bucket}/huddle-enterprise/{corpus}/exports/{date}/`

## Stack

- Python 3.11+ stdlib + `duckdb` + `pyarrow` + `boto3` + `fastapi` + `uvicorn` + `pdfplumber` or `pymupdf` (text extract)
- Headless `claude -p --output-format json --json-schema '{...}'` for structured extraction
- DuckDB native Parquet I/O; httpfs extension for direct S3 reads

## Wiki schema (DuckDB)

**corpora**: name PK, description, created_at, updated_at

**docs**: doc_id PK (sha256 of source+content), corpus FK, source_path, title, doc_type, num_pages, ingested_at, model

**chunks**: chunk_id PK (`{doc_id}#{ordinal}` or `{corpus}#manual#{slug}`), doc_id, corpus, ordinal, page_start, page_end, section, text, summary, concepts[], definitions JSON, claims[], examples[], xrefs[], created_at, updated_at

**qa**: qa_id PK, corpus, question, answer, dissent JSON, personas[], chunk_refs[], asked_at

## Conventions

- All scripts use `{PYTHON_BIN}` resolved once (reuse huddle's `global_state.py` pattern).
- Chunk ordinals are stable across re-ingests (deterministic chunker keyed on doc content hash + position).
- Manual chunks have `doc_id = '{corpus}#manual'` and ordinal allocated monotonically.
- HTTP API mirrors CLI 1:1 — same `storage.py` backs both.
- Never read `infra/vault/` unless task-scoped to a specific file.

## Headless Claude invocation

```bash
claude -p \
  --output-format json \
  --json-schema "$(cat extraction_schema.json)" \
  --model sonnet \
  --max-budget-usd 0.10 \
  --bare \
  "<extraction prompt + chunk text>"
```

Use `--bare` for clean, hook-free, deterministic runs from inside wiki-builder.

## First end-to-end test

```bash
wiki-builder ingest ~/Desktop/Society-of-Mind.pdf --corpus society-of-mind
wiki-builder stats society-of-mind
huddle ask society-of-mind "What is a K-line?"
responser serve --port 8087   # then open http://127.0.0.1:8087/
```

## Conversational architecture (binary tree + pre-warm + cache)

Every huddle answer returns a `next_branches` array of exactly two `{question, hook_in_answer, why_likely}` objects. The answer's prose is required to weave a subtle hook sentence for each branch — no "you might ask next" labels; the rhetoric itself invites the follow-up. After returning, `ask()` spawns two detached background jobs that pre-compute those two branches, one level deep (never recursive). On the next request, `find_cached_qa()` does a normalized exact-match lookup — cache hits return in milliseconds with `Answer.cached=True`. The UI surfaces the two branches as clickable chips below the answer; clicking submits → cache hit → instant.

Cost: primary answer + Amara (if triggered) + 2 speculative branches in background. Speed buys engagement.

## Answer voice

The `answer` field is read by senior audiences. The synthesis prompt (`TASK_INSTRUCTIONS` in `engine.py`) enforces speech-grade prose: paragraphs only, no bullets, no headers, no `**bold**`, named specifics over "many studies", banned phrasings like "In conclusion". Three short paragraphs, ends on a quotable verdict. `perspectives` and `dissent` keep each persona's own voice — the speech voice applies only to `answer`.

## Auto-research (Amara)

After the main answer, `_needs_research()` scans `dissent`, `open_questions`, and `perspectives` for research-worthy patterns (existence questions, named-buyer questions, current-state questions, pricing, adoption). If matched, `_amara_research()` runs a follow-up `claude -p --bare --allowedTools WebSearch --model haiku` with a strict 2-search cap. Findings come back with verdicts (`supports_answer` / `contradicts_answer` / `inconclusive`) and real URLs. The user adjudicates whether the room's concerns are settled.

Triggered by question shape (`RESEARCH_TRIGGERS` list), not by which persona was in the room.
