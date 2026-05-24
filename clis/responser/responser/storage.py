"""Ticket queue storage. DuckDB at ~/.config/.../huddle-enterprise/responser/tickets.duckdb"""
from __future__ import annotations
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

ENV_ROOT = "HUDDLE_ENTERPRISE_ROOT"


def _root() -> Path:
    override = os.environ.get(ENV_ROOT)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "muthuishere-agent-skills" / "huddle-enterprise"


def _db_path() -> Path:
    p = _root() / "responser"
    p.mkdir(parents=True, exist_ok=True)
    return p / "tickets.duckdb"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   TEXT PRIMARY KEY,
    corpus      TEXT NOT NULL,
    question    TEXT NOT NULL,
    asker       TEXT,
    status      TEXT NOT NULL,           -- queued | running | done | error
    answer      JSON,
    error       TEXT,
    created_at  TIMESTAMP NOT NULL,
    updated_at  TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_corpus  ON tickets(corpus);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect(read_only: bool = False):
    return duckdb.connect(str(_db_path()), read_only=read_only)


def init_schema() -> None:
    with _connect() as con:
        con.execute(SCHEMA_SQL)


def create_ticket(*, corpus: str, question: str, asker: str | None = None) -> str:
    init_schema()
    tid = uuid.uuid4().hex[:16]
    ts = _now()
    with _connect() as con:
        con.execute(
            "INSERT INTO tickets(ticket_id, corpus, question, asker, status, answer, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', NULL, NULL, ?, ?)",
            [tid, corpus, question, asker, ts, ts],
        )
    return tid


def set_status(ticket_id: str, status: str, *, error: str | None = None) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE tickets SET status = ?, error = ?, updated_at = ? WHERE ticket_id = ?",
            [status, error, _now(), ticket_id],
        )


def set_answer(ticket_id: str, answer: dict) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE tickets SET status = 'done', answer = ?, error = NULL, updated_at = ? WHERE ticket_id = ?",
            [json.dumps(answer), _now(), ticket_id],
        )


def get_ticket(ticket_id: str) -> dict[str, Any] | None:
    init_schema()
    with _connect(read_only=True) as con:
        rows = con.execute(
            "SELECT ticket_id, corpus, question, asker, status, answer, error, created_at, updated_at "
            "FROM tickets WHERE ticket_id = ?",
            [ticket_id],
        ).fetchall()
    if not rows:
        return None
    r = rows[0]
    cols = ["ticket_id", "corpus", "question", "asker", "status", "answer", "error",
            "created_at", "updated_at"]
    d = dict(zip(cols, r))
    if isinstance(d["answer"], str):
        try:
            d["answer"] = json.loads(d["answer"])
        except Exception:
            pass
    return d


def list_tickets(*, corpus: str | None = None, status: str | None = None,
                 limit: int = 50) -> list[dict[str, Any]]:
    init_schema()
    sql = ("SELECT ticket_id, corpus, question, asker, status, error, created_at, updated_at "
           "FROM tickets WHERE 1=1")
    params: list[Any] = []
    if corpus:
        sql += " AND corpus = ?"
        params.append(corpus)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect(read_only=True) as con:
        rows = con.execute(sql, params).fetchall()
    cols = ["ticket_id", "corpus", "question", "asker", "status", "error",
            "created_at", "updated_at"]
    return [dict(zip(cols, r)) for r in rows]
