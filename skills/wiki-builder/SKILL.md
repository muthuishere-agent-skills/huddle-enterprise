---
name: wiki-builder
description: >
  Build and curate an LLM-queryable wiki from documents. Trigger this skill when the user says anything like:
  "ingest this PDF", "build a wiki from this doc", "add to corpus X", "update the wiki",
  "patch chunk Y", "delete this chunk", "drop the corpus", "what corpora do we have",
  "what's in the wiki", "search the wiki for X", "stats on the wiki", "export the wiki to S3",
  "pull the wiki from S3", "start the wiki API", "serve the wiki", or any CRUD-style request
  about the LLM wiki / parquet store / DuckDB store. Also trigger when the user hands over a
  document file and asks to make it searchable, or when the autonomous huddle needs to consult
  the wiki for an answer.
---

# wiki-builder

`wiki-builder` is the lifecycle manager for the huddle-enterprise LLM wiki.

A "corpus" is a named knowledge bucket (e.g. `society-of-mind`, `acme-finance`, `policy-q3`). Each corpus has a local DuckDB store plus Parquet exports, and can be mirrored to S3.

## Where the CLI lives

```
{repo_root}/clis/wiki-builder/bin/wiki-builder
```

Resolve `{repo_root}` from the current directory upward (look for `huddle-enterprise/` containing `clis/wiki-builder/`). Then call it directly. Do NOT install with pip — the launcher already sets `PYTHONPATH`.

## When to use which subcommand

| User intent | Command |
|---|---|
| Make a new document searchable | `wiki-builder ingest <path> --corpus <name>` |
| What's available? | `wiki-builder list` |
| Health/size of a corpus | `wiki-builder stats <corpus>` |
| Find passages about X | `wiki-builder query <corpus> "<question>" --k 5` |
| Inspect one chunk | `wiki-builder get-chunk <corpus> <chunk_id>` |
| Manually add curated knowledge (no LLM call) | `wiki-builder add-chunk <corpus> --json '...'` |
| Fix a chunk (typo, wrong concept) | `wiki-builder update-chunk <corpus> <id> --json '{...}'` |
| Remove a bad chunk | `wiki-builder delete-chunk <corpus> <id>` |
| Remove a whole document | `wiki-builder delete-doc <corpus> <doc_id>` |
| Nuke a corpus | `wiki-builder delete-corpus <corpus> --yes` |
| Snapshot to disk | `wiki-builder export <corpus>` |
| Push to S3 | `wiki-builder export <corpus> --to s3://bucket/prefix` |
| Hydrate from S3 | `wiki-builder pull <corpus> --from s3://bucket/prefix` |
| Run the HTTP API (for external tools / UIs) | `wiki-builder serve --port 8765` |

## How to interpret output

All subcommands print JSON to stdout (except `query` which has a condensed view; pass `--full` for JSON). Errors go to stderr with non-zero exit.

Ingest result fields:
- `doc_id` — stable hash; reuse to update or delete the doc later
- `chunks` — total chunks written
- `extract_failures` — count of chunks where claude couldn't extract; these still get stored with `[extraction failed: ...]` as summary

## State location

```
~/.config/muthuishere-agent-skills/huddle-enterprise/{corpus}/
├── wiki.duckdb        # primary store
├── exports/*.parquet  # parquet snapshots (auto-written on every mutation)
└── qa/                # past Q&A (written by the autonomous huddle)
```

Override the root with `HUDDLE_ENTERPRISE_ROOT` (useful for tests).

## HTTP API

`wiki-builder serve` exposes the same operations over REST:

```
GET    /corpora
POST   /corpora                            {name, description}
DELETE /corpora/{c}?confirm=true
GET    /corpora/{c}/stats
GET    /corpora/{c}/docs
DELETE /corpora/{c}/docs/{doc_id}
POST   /corpora/{c}/ingest                 {path, model, workers}     [async]
POST   /corpora/{c}/upload                 multipart file              [async]
GET    /corpora/{c}/chunks?doc=...
POST   /corpora/{c}/chunks                 ChunkCreate
GET    /corpora/{c}/chunks/{id}
PUT    /corpora/{c}/chunks/{id}            ChunkPatch
DELETE /corpora/{c}/chunks/{id}
POST   /corpora/{c}/search                 {q, k}
POST   /corpora/{c}/export                 {s3_uri?, profile?}
POST   /corpora/{c}/pull                   {s3_uri, profile?}
GET    /corpora/{c}/qa
```

The API is the right surface when:
- A UI / Responder app needs to read/edit the wiki
- A non-Claude-Code system needs to push curated knowledge in
- Multiple machines share one S3-backed corpus

## Manual curation pattern

Sometimes a document gets ingested with errors (concept mis-labeled, summary wrong). Don't re-ingest — patch:

```bash
wiki-builder update-chunk society-of-mind 8ca07710b790b932#0000 \
  --json '{"concepts": ["Society of Mind", "mental agents", "K-lines"]}'
```

For knowledge that isn't in any source document (policy decisions, oral history, FAQ answers), use the manual chunk path. Manual chunks live under `doc_id = "{corpus}#manual"`:

```bash
wiki-builder add-chunk acme-finance --json '{
  "text": "Travel reimbursements over $500 require pre-approval from a director.",
  "section": "Travel Policy",
  "concepts": ["travel", "reimbursement", "approval"],
  "definitions": {"reimbursement": "..."}
}'
```

## Headless-Claude usage

The `ingest` path calls `claude -p --bare --output-format json --json-schema ... --model sonnet` per chunk. Concurrency defaults to 4; raise with `--workers`. Per-call budget cap is `--max-budget-usd 0.10` (in `extract.py`); tune if you hit caps on large corpora.

## When NOT to use this skill

- For one-off lookups in code or git history — use `grep` / `git log`.
- For state about the current huddle conversation — that lives in `huddle/`, not the wiki.
