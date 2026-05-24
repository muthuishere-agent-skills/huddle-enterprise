"""Responser API + UI."""
from __future__ import annotations
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import storage, worker


def _ensure_sibling_packages() -> None:
    here = Path(__file__).resolve()
    clis = here.parents[2]
    for sib in ("wiki-builder", "huddle"):
        p = clis / sib
        if p.exists() and str(p) not in sys.path:
            sys.path.insert(0, str(p))


_ensure_sibling_packages()
from wiki_builder import storage as wiki_storage  # noqa: E402

app = FastAPI(title="responser", version="0.1.0")


# ---------- request models ----------

class AskRequest(BaseModel):
    corpus: str
    question: str
    asker: str | None = None


# ---------- corpora (read-through to wiki-builder) ----------

@app.get("/api/corpora")
def list_corpora():
    return wiki_storage.list_corpora()


# ---------- tickets ----------

@app.post("/api/tickets", status_code=202)
def create_ticket(body: AskRequest, tasks: BackgroundTasks):
    # Sanity-check corpus exists.
    corpora = {c["corpus"] for c in wiki_storage.list_corpora()}
    if body.corpus not in corpora:
        raise HTTPException(404, f"corpus not found: {body.corpus}")
    if not body.question.strip():
        raise HTTPException(400, "question is required")
    tid = storage.create_ticket(
        corpus=body.corpus, question=body.question, asker=body.asker,
    )
    tasks.add_task(worker.run, tid, body.corpus, body.question)
    return {"ticket_id": tid, "status": "queued"}


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    t = storage.get_ticket(ticket_id)
    if not t:
        raise HTTPException(404, "ticket not found")
    return t


@app.get("/api/tickets")
def list_tickets(corpus: str | None = None, status: str | None = None, limit: int = 50):
    return storage.list_tickets(corpus=corpus, status=status, limit=limit)


# ---------- UI ----------

UI_INDEX = Path(__file__).resolve().parents[3] / "apps" / "responser-web" / "index.html"


@app.get("/")
def index():
    if not UI_INDEX.exists():
        raise HTTPException(500, f"UI not found at {UI_INDEX}")
    return FileResponse(str(UI_INDEX))
