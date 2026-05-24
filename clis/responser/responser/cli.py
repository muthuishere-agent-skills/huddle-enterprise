"""Responser CLI: serve, list, recent."""
from __future__ import annotations
import argparse
import json
import sys

from . import storage


def _print_json(obj) -> None:
    print(json.dumps(obj, default=str, indent=2))


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from .api import app
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    _print_json(storage.list_tickets(
        corpus=args.corpus, status=args.status, limit=args.limit,
    ))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    t = storage.get_ticket(args.ticket_id)
    if not t:
        print("not found", file=sys.stderr); return 1
    _print_json(t)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="responser", description="Responser: question→answer queue + UI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="start the API + UI on a port")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--log-level", default="info")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("list", help="list tickets")
    sp.add_argument("--corpus")
    sp.add_argument("--status")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("get", help="get one ticket")
    sp.add_argument("ticket_id")
    sp.set_defaults(func=cmd_get)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
