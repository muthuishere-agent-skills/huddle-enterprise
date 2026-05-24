"""CLI: huddle ask <corpus> "<question>" [--k N] [--json]"""
from __future__ import annotations
import argparse
import json
import sys

from .engine import ask, HuddleError


def _print_json(obj) -> None:
    print(json.dumps(obj, default=str, indent=2))


def cmd_ask(args: argparse.Namespace) -> int:
    try:
        ans = ask(
            args.corpus,
            args.question,
            k=args.k,
            model=args.model,
            record=not args.no_record,
            research=not args.no_research,
            use_cache=not args.no_cache,
            speculative=not args.no_speculative,
        )
    except HuddleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.format == "json":
        _print_json(ans.to_dict())
        return 0

    # Human-readable
    cache_tag = "  (cached)" if ans.cached else ""
    print(f"Q: {ans.question}{cache_tag}\n")
    print(f"A: {ans.answer}\n")
    print(f"— grounded in: {', '.join(ans.chunk_refs) or '(no wiki chunks)'}")
    print(f"— personas: {', '.join(ans.personas_chosen)}  (confidence: {ans.confidence})\n")
    if ans.perspectives:
        print("Perspectives:")
        for p in ans.perspectives:
            print(f"  [{p['persona']}] {p['view']}")
        print()
    if ans.dissent:
        print("Dissent:")
        for d in ans.dissent:
            print(f"  [{d['persona']}] {d['concern']}")
        print()
    if ans.open_questions:
        print("Open questions:")
        for q in ans.open_questions:
            print(f"  - {q}")
        print()
    if ans.amara_research:
        ar = ans.amara_research
        print("Amara research (auto-triggered):")
        if ar.get("error"):
            print(f"  [error] {ar['error']}")
        for f in ar.get("findings", []):
            verdict_tag = {
                "supports_answer":     "✓",
                "contradicts_answer":  "✗",
                "inconclusive":        "?",
            }.get(f.get("verdict"), "?")
            print(f"  {verdict_tag} [{f.get('verdict','?')}] {f.get('open_question','')[:140]}")
            print(f"      {f.get('evidence','')}")
            for s in f.get("sources", []):
                print(f"      · {s.get('title','')} — {s.get('url','')}")
        if ar.get("named_buyers"):
            print(f"  named buyers: {', '.join(ar['named_buyers'])}")
        if ar.get("summary"):
            print(f"  summary: {ar['summary']}")
        print()
    if ans.next_branches:
        print("Likely next questions (pre-warming in background):")
        for b in ans.next_branches:
            print(f"  → {b.get('question','')}")
            if b.get("hook_in_answer"):
                print(f"     hook: \"{b['hook_in_answer'][:120]}\"")
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    # Cross-package import to read past QA
    from wiki_builder import storage as wiki_storage
    with wiki_storage.Store(args.corpus, read_only=True) as s:
        rows = s.recent_qa(limit=args.limit)
    _print_json(rows)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="huddle", description="Autonomous huddle: ask the wiki.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ask", help="autonomous huddle answer for a question against a corpus")
    sp.add_argument("corpus")
    sp.add_argument("question")
    sp.add_argument("--k", type=int, default=5, help="top-K wiki chunks to retrieve")
    sp.add_argument("--model", default="sonnet")
    sp.add_argument("--format", choices=["human", "json"], default="human")
    sp.add_argument("--no-record", action="store_true", help="don't write to QA store")
    sp.add_argument("--no-research", action="store_true",
                    help="skip Amara's auto web-research pass")
    sp.add_argument("--no-cache", action="store_true",
                    help="bypass the QA cache and re-compute even if a recent answer exists")
    sp.add_argument("--no-speculative", action="store_true",
                    help="don't pre-warm next-branch answers in the background")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("recent", help="show recent Q&A for a corpus")
    sp.add_argument("corpus")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_recent)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
