"""In-process worker. Called by BackgroundTasks; runs the huddle and stores answer.

Cross-package import: we need huddle_engine and wiki_builder on sys.path.
"""
from __future__ import annotations
import sys
import traceback
from pathlib import Path

from . import storage


def _ensure_sibling_packages() -> None:
    here = Path(__file__).resolve()
    clis = here.parents[2]  # .../clis/
    for sibling in ("huddle/", "wiki-builder/"):
        p = clis / sibling.rstrip("/")
        if p.exists() and str(p) not in sys.path:
            sys.path.insert(0, str(p))


_ensure_sibling_packages()
from huddle_engine.engine import ask, HuddleError  # noqa: E402


def run(ticket_id: str, corpus: str, question: str, *, k: int = 5,
        model: str = "sonnet") -> None:
    """Synchronous worker — called inside a BackgroundTasks coroutine.

    Updates the ticket row through queued → running → done | error.
    """
    storage.set_status(ticket_id, "running")
    try:
        ans = ask(corpus, question, k=k, model=model, record=True)
        storage.set_answer(ticket_id, ans.to_dict())
    except HuddleError as e:
        storage.set_status(ticket_id, "error", error=str(e))
    except Exception as e:
        storage.set_status(
            ticket_id, "error",
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}",
        )
