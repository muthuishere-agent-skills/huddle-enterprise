"""CLI surface for wiki-builder. Subcommands map 1:1 to storage ops."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from . import storage as st
from .ingest import ingest_file, default_progress
from . import s3_sync


def _print_json(obj) -> None:
    print(json.dumps(obj, default=str, indent=2))


def cmd_ingest(args: argparse.Namespace) -> int:
    report = ingest_file(
        Path(args.path),
        corpus=args.corpus,
        model=args.model,
        max_workers=args.workers,
        on_progress=None if args.quiet else default_progress,
    )
    _print_json({
        "doc_id": report.doc_id,
        "title": report.title,
        "pages": report.num_pages,
        "chunks": report.num_chunks,
        "extract_failures": report.extract_failures,
        "elapsed_s": round(report.elapsed_s, 2),
    })
    if args.s3:
        pushed = s3_sync.push(args.corpus, args.s3, profile=args.profile)
        _print_json({"uploaded": pushed})
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _print_json(st.list_corpora())
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with st.Store(args.corpus, read_only=True) as s:
        _print_json(s.stats())
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    with st.Store(args.corpus, read_only=True) as s:
        results = s.search(args.q, k=args.k)
    for r in results:
        # condensed view by default; --full for everything
        if args.full:
            _print_json(r)
        else:
            print(f"[{r['score']}] {r['chunk_id']}  pp.{r.get('page_start')}-{r.get('page_end')}")
            if r.get("summary"):
                print(f"    {r['summary']}")
            if r.get("concepts"):
                print(f"    concepts: {', '.join(r['concepts'][:6])}")
            print()
    if not results:
        print("(no matches)", file=sys.stderr)
    return 0


def cmd_get_chunk(args: argparse.Namespace) -> int:
    with st.Store(args.corpus, read_only=True) as s:
        c = s.get_chunk(args.chunk_id)
    if not c:
        print("not found", file=sys.stderr); return 1
    _print_json(c)
    return 0


def cmd_list_chunks(args: argparse.Namespace) -> int:
    with st.Store(args.corpus, read_only=True) as s:
        rows = s.list_chunks(doc_id=args.doc, limit=args.limit, offset=args.offset)
    _print_json(rows)
    return 0


def cmd_add_chunk(args: argparse.Namespace) -> int:
    """Manually add a chunk (no claude call). For curated knowledge."""
    payload = json.loads(args.json) if args.json else {}
    text = payload.get("text") or (Path(args.from_file).read_text() if args.from_file else "")
    if not text:
        print("text required (via --json or --from-file)", file=sys.stderr); return 1
    with st.Store(args.corpus) as s:
        doc_id = f"{args.corpus}#manual"
        # ensure manual doc row exists
        s.upsert_doc(
            doc_id=doc_id, source_path=None, title="manual",
            doc_type="manual", num_pages=None, model=None,
        )
        existing = s.list_chunks(doc_id=doc_id, limit=1_000_000)
        ordinal = max([c["ordinal"] for c in existing], default=-1) + 1
        slug = st.slugify(payload.get("section") or text[:40])
        chunk = st.Chunk(
            chunk_id=f"{doc_id}#{ordinal:04d}-{slug}",
            doc_id=doc_id,
            corpus=args.corpus,
            ordinal=ordinal,
            text=text,
            section=payload.get("section"),
            summary=payload.get("summary"),
            concepts=payload.get("concepts") or [],
            definitions=payload.get("definitions") or {},
            claims=payload.get("claims") or [],
            examples=payload.get("examples") or [],
            xrefs=payload.get("xrefs") or [],
        )
        s.upsert_chunk(chunk)
        s.export_parquet()
    _print_json({"chunk_id": chunk.chunk_id, "ordinal": ordinal})
    return 0


def cmd_update_chunk(args: argparse.Namespace) -> int:
    patch = json.loads(args.json)
    with st.Store(args.corpus) as s:
        ok = s.patch_chunk(args.chunk_id, patch)
        if ok:
            s.export_parquet()
    if not ok:
        print("chunk not found or no allowed fields in patch", file=sys.stderr); return 1
    _print_json({"updated": args.chunk_id})
    return 0


def cmd_delete_chunk(args: argparse.Namespace) -> int:
    with st.Store(args.corpus) as s:
        ok = s.delete_chunk(args.chunk_id)
        if ok:
            s.export_parquet()
    if not ok:
        print("chunk not found", file=sys.stderr); return 1
    _print_json({"deleted": args.chunk_id})
    return 0


def cmd_delete_doc(args: argparse.Namespace) -> int:
    with st.Store(args.corpus) as s:
        n = s.delete_doc(args.doc_id)
        if n:
            s.export_parquet()
    _print_json({"deleted_doc": args.doc_id, "removed_rows": n})
    return 0


def cmd_delete_corpus(args: argparse.Namespace) -> int:
    if not args.yes:
        print(f"refusing to delete corpus '{args.corpus}' without --yes", file=sys.stderr)
        return 1
    ok = st.drop_corpus(args.corpus)
    _print_json({"deleted_corpus": args.corpus, "existed": ok})
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with st.Store(args.corpus, read_only=True) as s:
        targets = s.export_parquet()
    _print_json({k: str(v) for k, v in targets.items()})
    if args.to:
        pushed = s3_sync.push(args.corpus, args.to, profile=args.profile)
        _print_json({"uploaded": pushed})
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    files = s3_sync.pull(args.corpus, args.frm, profile=args.profile)
    _print_json({"downloaded": files})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api import app
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wiki-builder", description="LLM wiki: build, curate, serve.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ingest", help="ingest a PDF/MD/TXT into a corpus")
    sp.add_argument("path")
    sp.add_argument("--corpus", required=True)
    sp.add_argument("--model", default="sonnet")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--s3", help="optional s3://bucket/prefix to push parquet after ingest")
    sp.add_argument("--profile", help="AWS profile")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("list", help="list corpora")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("stats", help="stats for a corpus")
    sp.add_argument("corpus")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("query", help="search a corpus")
    sp.add_argument("corpus")
    sp.add_argument("q")
    sp.add_argument("--k", type=int, default=5)
    sp.add_argument("--full", action="store_true")
    sp.set_defaults(func=cmd_query)

    sp = sub.add_parser("get-chunk", help="show one chunk")
    sp.add_argument("corpus")
    sp.add_argument("chunk_id")
    sp.set_defaults(func=cmd_get_chunk)

    sp = sub.add_parser("list-chunks", help="list chunks (optionally filtered by doc)")
    sp.add_argument("corpus")
    sp.add_argument("--doc")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--offset", type=int, default=0)
    sp.set_defaults(func=cmd_list_chunks)

    sp = sub.add_parser("add-chunk", help="manually add a chunk (no extraction call)")
    sp.add_argument("corpus")
    sp.add_argument("--json", help='inline JSON: {"text":"...", "concepts":[...], ...}')
    sp.add_argument("--from-file", help="read chunk text from file")
    sp.set_defaults(func=cmd_add_chunk)

    sp = sub.add_parser("update-chunk", help="patch a chunk")
    sp.add_argument("corpus")
    sp.add_argument("chunk_id")
    sp.add_argument("--json", required=True, help='patch JSON (allowed fields only)')
    sp.set_defaults(func=cmd_update_chunk)

    sp = sub.add_parser("delete-chunk", help="delete one chunk")
    sp.add_argument("corpus")
    sp.add_argument("chunk_id")
    sp.set_defaults(func=cmd_delete_chunk)

    sp = sub.add_parser("delete-doc", help="delete a doc and all its chunks")
    sp.add_argument("corpus")
    sp.add_argument("doc_id")
    sp.set_defaults(func=cmd_delete_doc)

    sp = sub.add_parser("delete-corpus", help="drop a whole corpus directory")
    sp.add_argument("corpus")
    sp.add_argument("--yes", action="store_true", help="confirm")
    sp.set_defaults(func=cmd_delete_corpus)

    sp = sub.add_parser("export", help="write parquet snapshots; optionally push to S3")
    sp.add_argument("corpus")
    sp.add_argument("--to", help="s3://bucket/prefix")
    sp.add_argument("--profile")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("pull", help="download parquet snapshots from S3 to local exports/")
    sp.add_argument("corpus")
    sp.add_argument("--from", dest="frm", required=True, help="s3://bucket/prefix")
    sp.add_argument("--profile")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("serve", help="run the HTTP API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--log-level", default="info")
    sp.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
